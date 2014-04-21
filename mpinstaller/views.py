import json
import logging
from urllib import quote
from distutils.version import LooseVersion
from django.conf import settings
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render_to_response
from mpinstaller.auth import SalesforceOAuth2
from mpinstaller.installer import version_install_map
from mpinstaller.installer import install_map_to_package_list
from mpinstaller.installer import install_map_to_json
from mpinstaller.mdapi import ApiInstallVersion
from mpinstaller.mdapi import ApiUninstallVersion
from mpinstaller.mdapi import ApiListMetadata
from mpinstaller.mdapi import ApiRetrieveInstalledPackages
from mpinstaller.models import Package
from mpinstaller.models import PackageInstallation
from mpinstaller.models import PackageInstallationSession
from mpinstaller.models import PackageInstallationStep
from mpinstaller.models import PackageVersion
from mpinstaller.package import PackageZipBuilder
from simple_salesforce import Salesforce

logger = logging.getLogger(__name__)

def package_overview(request, namespace, beta=None):
    package = get_object_or_404(Package, namespace = namespace)

    if beta:
        suffix = 'beta'
    else:
        suffix = 'prod'

    current_version = getattr(package, 'current_%s' % suffix)
    if current_version:
        return package_version_overview(request, namespace, current_version.id)

    return render_to_response('mpinstaller/package_overview_no_version.html', {'package': package, 'beta': beta})

def package_version_overview(request, namespace, version_id):
    version = get_object_or_404(PackageVersion, package__namespace = namespace, id=version_id)

    oauth = request.session.get('oauth')

    request.session['mpinstaller_current_version'] = version.id

    install_map = []
    package_list = []
    if oauth and oauth.get('access_token', None):
        org_packages = request.session.get('org_packages', {})
        metadata = request.session.get('metadata', {})
        # Get the install map and package list
        install_map = version_install_map(version, org_packages, metadata)
        package_list = install_map_to_package_list(install_map)
    
    logged_in = False
    redirect = quote(request.build_absolute_uri(request.path))
    if oauth and oauth.get('access_token', None):
        login_url = None
        logout_url = request.build_absolute_uri('/mpinstaller/oauth/logout?redirect=%s' % redirect)
    else:
        login_url = request.build_absolute_uri('/mpinstaller/oauth/login?redirect=%s' % redirect)
        logout_url = None

    install_url = request.build_absolute_uri('/mpinstaller/%s/version/%s/install' % (namespace, version_id))

    data = {
        'version': version,
        'oauth': request.session.get('oauth',None),
        'login_url': login_url,
        'logout_url': logout_url,
        'install_url': install_url,
        'base_url': request.build_absolute_uri('/mpinstaller/'),
        'package_list': package_list,
        'install_map': install_map,
    }

    return render_to_response('mpinstaller/package_version_overview.html', data)

def start_package_installation(request, namespace, version_id):
    """ Kicks off a package installation and redirects to the installation's page """
    version = get_object_or_404(PackageVersion, package__namespace=namespace, id=version_id)
    oauth = request.session.get('oauth', None)

    # Redirect back to the package overview page if not connected to an org
    if not oauth or not oauth.get('access_token'):
        redirect = version.get_installer_url(request)
        return HttpResponseRedirect(redirect)
   
    # This view should only be used for executing a map already reviewed by the user.
    # If there is no installed list or metadata list in session, that didn't happen for some reason 
    installed = request.session.get('org_packages', None)
    if installed is None:
        return HttpResponseRedirect(version.get_installer_url(request))
    metadata = request.session.get('metadata', None)
    if metadata is None:
        return HttpResponseRedirect(version.get_installer_url(request))

    install_map = version_install_map(version, installed, metadata)

    installation_obj = PackageInstallation(
        package = version.package,
        version = version,
        org_id = oauth['org_id'],
        org_type = oauth['org_type'],
        status = 'Pending',
        username = oauth['username'],
        install_map = install_map_to_json(install_map),
    )
    installation_obj.save()

    # Temporarily save the needed session variables so background processes can do the work
    session_obj = PackageInstallationSession(
        installation = installation_obj,
        oauth = json.dumps(oauth),
        org_packages = json.dumps(installed),
        metadata = json.dumps(request.session.get('metadata', {})),
    )
    session_obj.save()

    for step in install_map:
        step_obj = PackageInstallationStep(
            installation = installation_obj,
            package = step['version'].package,
            version = step['version'],
            previous_version = step['installed'],
            action = step['action'],
            status = 'Pending',
        )
        if step_obj.action == 'skip':
            step_obj.status = 'Succeeded'
        step_obj.save()

    return HttpResponseRedirect('/mpinstaller/installation/%s' % installation_obj.id)

def installation_overview(request, installation_id):
    installation = get_object_or_404(PackageInstallation, id=installation_id)

    oauth = request.session.get('oauth')

    request.session['mpinstaller_current_version'] = installation.version.id

    redirect = quote(request.build_absolute_uri(request.path))
    if oauth and oauth.get('access_token', None):
        login_url = None
        logout_url = request.build_absolute_uri('/mpinstaller/oauth/logout?redirect=%s' % redirect)
    else:
        login_url = request.build_absolute_uri('/mpinstaller/oauth/login?redirect=%s' % redirect)
        logout_url = None

    data = {
        'installation': installation,
        'version': installation.version,
        'oauth': request.session.get('oauth',None),
        'login_url': login_url,
        'logout_url': logout_url,
        'base_url': request.build_absolute_uri('/mpinstaller/'),
    }

    return render_to_response('mpinstaller/installation_overview.html', data)

def package_installation_overview(request, installation_id):
    """ Shows information about a package installation """
    installation = get_object_or_404(PackageInstallation, id=installation_id)

    return render_to_response('mpinstaller/package_installation.html', {'installation': installation})
     
def oauth_login(request):
    """ Redirects the user to the appropriate login page for OAuth2 login """
    redirect = request.GET['redirect']

    sandbox = request.GET.get('sandbox', False)
    if sandbox == 'true':
        sandbox = True

    if not request.session.get('oauth', None):
        request.session['oauth'] = {}
  
    request.session['oauth']['sandbox'] = sandbox

    scope = request.GET.get('scope', quote('full refresh_token'))

    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token', None):
        sf = SalesforceOAuth2(settings.MPINSTALLER_CLIENT_ID, settings.MPINSTALLER_CLIENT_SECRET, settings.MPINSTALLER_CALLBACK_URL, sandbox=sandbox)
        request.session['mpinstaller_redirect'] = redirect 
        return HttpResponseRedirect(sf.authorize_url(scope=scope))

    return HttpResponseRedirect(redirect)

def oauth_callback(request):
    """ Handles the callback from Salesforce after a successful OAuth2 login """
    oauth = request.session.get('oauth', {})
    sandbox = oauth.get('sandbox', False)
    sf = SalesforceOAuth2(settings.MPINSTALLER_CLIENT_ID, settings.MPINSTALLER_CLIENT_SECRET, settings.MPINSTALLER_CALLBACK_URL, sandbox=sandbox)

    code = request.GET.get('code',None)
    if not code:
        return HttpResponse('ERROR: No code provided')

    resp = sf.get_token(code)

    # Call the REST API to get the org name for display on screen
    org = get_oauth_org(resp)

    resp['org_id'] = org['Id']
    resp['org_name'] = org['Name']
    resp['org_type'] = org['OrganizationType']

    # Append (Sandbox) to org type if sandbox
    if sandbox:
        resp['org_type'] = '%s (Sandbox)' % resp['org_type']

    # Call the REST API to get the user's login for display on screen
    user = get_oauth_user(resp)
    resp['username'] = user['Username']
    resp['perm_modifyalldata'] = user['Profile']['PermissionsModifyAllData']

    # Log the info
    logger.info(resp)

    # Set the response in the session
    request.session['oauth'] = resp

    return HttpResponseRedirect(request.build_absolute_uri('/mpinstaller/oauth/post_login'))

def oauth_post_login(request):
    """ After successful oauth login, the user is redirected to this view which shows
        the status of fetching needed info from their org to determine install steps """

    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token'):
        return HttpResponse('Unauthorized', status=401)

    version = None
    version_id = request.session.get('mpinstaller_current_version', None)
    if version_id:
        version = PackageVersion.objects.get(id=version_id)

    redirect = request.session.get('mpinstaller_redirect', None)
    if not redirect and version:
        redirect = version.get_installer_url(request)
    message = None

    # Setup the list of actions to take after page load
    actions = []
    actions.append({
        'url': request.build_absolute_uri('/mpinstaller/org/user'),
        'message': 'Fetching user info',
    })
    actions.append({
        'url': request.build_absolute_uri('/mpinstaller/org/org'),
        'message': 'Fetching org info',
    })
    actions.append({
        'url': request.build_absolute_uri('/mpinstaller/org/packages'),
        'message': 'Fetching installed packages',
    })
    if version:
        actions.append({
            'url': request.build_absolute_uri('/mpinstaller/org/condition_metadata/%s' % version.id),
            'message': 'Fetching metadata lists needed by the installation',
        })

    return render_to_response('mpinstaller/oauth_post_login.html', {
        'redirect': redirect, 
        'actions': actions,
        'oauth': oauth,
        'version': version,
    })

def org_user(request):
    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token'):
        return HttpResponse('Unauthorized', status=401)

    # Fetch user info from org
    user = get_oauth_user(oauth)
    oauth['username'] = user['Username']
    oauth['perm_modifyalldata'] = user['Profile']['PermissionsModifyAllData']
    request.session['oauth'] = oauth
    return HttpResponse('OK')

def org_org(request):
    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token'):
        return HttpResponse('Unauthorized', status=401)

    # Fetch org info from org
    org = get_oauth_org(oauth)
    oauth['org_id'] = org['Id']
    oauth['org_name'] = org['Name']
    oauth['org_type'] = org['OrganizationType']
    
    # Append (Sandbox) to org type if sandbox
    if oauth.get('sandbox',False):
        oauth['org_type'] = '%s (Sandbox)' % oauth['org_type']

    request.session['oauth'] = oauth
    return HttpResponse('OK')

def org_packages(request):
    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token'):
        return HttpResponse('Unauthorized', status=401)

    packages = get_org_packages(oauth)
    request.session['org_packages'] = packages
    return HttpResponse('OK')

def org_condition_metadata(request, version_id):
    oauth = request.session.get('oauth', None)
    if not oauth or not oauth.get('access_token'):
        return HttpResponse('Unauthorized', status=401)

    version = get_object_or_404(PackageVersion, id=version_id)
    metadata = get_org_metadata_for_conditions(version, oauth, request.session.get('metadata', {}))
    request.session['metadata'] = metadata
    return HttpResponse('OK')

def oauth_logout(request):
    """ Revoke the login token """
    redirect = request.GET['redirect']

    oauth = request.session.get('oauth', None)
        
    if oauth and oauth.get('access_token', None):
        sf = SalesforceOAuth2(settings.MPINSTALLER_CLIENT_ID, settings.MPINSTALLER_CLIENT_SECRET, settings.MPINSTALLER_CALLBACK_URL)
        sf.revoke_token(oauth['access_token'])
        del request.session['oauth']

    if request.session.get('org_packages', None) != None:
        del request.session['org_packages']

    if request.session.get('metadata', None) != None:
        del request.session['metadata']

    return HttpResponseRedirect(redirect)

def get_oauth_org(oauth):
    """ Fetches the org info from the org """
    if not oauth or not oauth.get('access_token', None):
        return 'Not connected'
    sf = Salesforce(instance_url = oauth['instance_url'], session_id = oauth['access_token'])

    # Parse org id from id which ends in /ORGID/USERID
    org_id = oauth['id'].split('/')[-2]

    org = sf.Organization.get(org_id)
    return org

def get_oauth_user(oauth):
    """ Fetches the user info from the org """
    if not oauth or not oauth.get('access_token', None):
        return 'Not connected'
    sf = Salesforce(instance_url = oauth['instance_url'], session_id = oauth['access_token'])
    # Parse user id from id which ends in /ORGID/USERID
    user_id = oauth['id'].split('/')[-1]

    #user = sf.User.get(user_id)
    res = sf.query("SELECT Id, Username, Profile.PermissionsModifyAllData from User WHERE Id='%s'" % user_id)
    user = res['records'][0];
    return user

def get_org_packages(oauth):
    """ Fetches all InstalledPackage objects (i.e. managed packages) in the org """
    api = ApiRetrieveInstalledPackages(oauth)
    packages = api()
    return packages

def get_org_metadata_for_conditions(version, oauth, metadata=None):
    """ Fetches metadata lists for all conditions used to install the current version """
    if not metadata:
        metadata = {}
    # Handle conditions on the main version
    for condition in version.conditions.all():
        if not metadata.has_key(condition.metadata_type):
            # Fetch the metadata for this type
            api = ApiListMetadata(oauth, condition.metadata_type, metadata)
            metadata[condition.metadata_type] = api()

    # Handle conditions on any dependent versions
    for dependency in version.dependencies.all():
        for condition in dependency.requires.conditions.all():
            if not metadata.has_key(condition.metadata_type):
                # Fetch the metadata for this type
                api = ApiListMetadata(oauth, condition.metadata_type, metadata)
                metadata[condition.metadata_type] = api()
        
    return metadata
    
def package_dependencies(request, namespace, beta=None):
    """ Returns package dependencies as json via GET and updates them via POST """
    package = get_object_or_404(Package, namespace=namespace)

    if request.method == 'POST':
        if not package.key or package.key != request.META.get('HTTP_AUTHORIZATION', None):
            return HttpResponse('Unauthorized', status=401)
        dependencies = json.loads(request.body)
        new_dependencies = package.update_dependencies(beta, dependencies)
        return HttpResponse(json.dumps(new_dependencies), content_type='application/json')
    else:
        # For GET requests, return the current dependencies
        return HttpResponse(json.dumps(package.get_dependencies(beta)), content_type='application/json')
