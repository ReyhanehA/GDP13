# -*- coding: utf-8 -*-
import logging
import httplib as http
import math
from itertools import islice

from flask import request
from modularodm import Q
from modularodm.exceptions import ModularOdmException, ValidationValueError, NoResultsFound

from framework import status
from framework.utils import iso8601format
from framework.mongo import StoredObject
from framework.flask import redirect
from framework.auth.decorators import must_be_logged_in, collect_auth
from framework.exceptions import HTTPError, PermissionsError

from website import language

from website.util import paths
from website.util import rubeus
from website.exceptions import NodeStateError
from website.project import new_node, new_private_link
from website.project.decorators import (
    must_be_contributor_or_public_but_not_anonymized,
    must_be_contributor_or_public,
    must_be_valid_project,
    must_have_permission,
    must_not_be_registration,
    http_error_if_disk_saving_mode
)
from website.tokens import process_token_or_pass
from website.util.permissions import ADMIN, READ, WRITE, CREATOR_PERMISSIONS
from website.util.rubeus import collect_addon_js
from website.project.model import has_anonymous_link, get_pointer_parent, NodeUpdateError, validate_title, Institution
from website.project.forms import NewNodeForm
from website.project.metadata.utils import serialize_meta_schemas
from website.models import Node, Pointer, WatchConfig, PrivateLink, Comment
from website import settings
from website.views import _render_nodes, find_bookmark_collection, validate_page_num
from website.profile import utils
from website.project.licenses import serialize_node_license_record
from website.util.sanitize import strip_html
from website.util import rapply

r_strip_html = lambda collection: rapply(collection, strip_html)
logger = logging.getLogger(__name__)

@must_be_valid_project
@must_have_permission(WRITE)
@must_not_be_registration
def edit_node(auth, node, **kwargs):
    post_data = request.json
    edited_field = post_data.get('name')
    value = post_data.get('value', '')

    if edited_field == 'title':
        try:
            node.set_title(value, auth=auth)
        except ValidationValueError as e:
            raise HTTPError(
                http.BAD_REQUEST,
                data=dict(message_long=e.message)
            )
    elif edited_field == 'description':
        node.set_description(value, auth=auth)
    node.save()
    return {'status': 'success'}


##############################################################################
# New Project
##############################################################################


@must_be_logged_in
def project_new(**kwargs):
    return {}

@must_be_logged_in
def project_new_post(auth, **kwargs):
    user = auth.user

    data = request.get_json()
    title = strip_html(data.get('title'))
    title = title.strip()
    category = data.get('category', 'project')
    template = data.get('template')
    description = strip_html(data.get('description'))
    new_project = {}

    if template:
        original_node = Node.load(template)
        changes = {
            'title': title,
            'category': category,
            'template_node': original_node,
        }

        if description:
            changes['description'] = description

        project = original_node.use_as_template(
            auth=auth,
            changes={
                template: changes,
            }
        )

    else:
        try:
            project = new_node(category, title, user, description)
        except ValidationValueError as e:
            raise HTTPError(
                http.BAD_REQUEST,
                data=dict(message_long=e.message)
            )
        new_project = _view_project(project, auth)
    return {
        'projectUrl': project.url,
        'newNode': new_project['node'] if new_project else None
    }, http.CREATED


@must_be_logged_in
@must_be_valid_project
def project_new_from_template(auth, node, **kwargs):
    new_node = node.use_as_template(
        auth=auth,
        changes=dict(),
    )
    return {'url': new_node.url}, http.CREATED, None


##############################################################################
# New Node
##############################################################################

@must_be_valid_project
@must_have_permission(WRITE)
@must_not_be_registration
def project_new_node(auth, node, **kwargs):
    form = NewNodeForm(request.form)
    user = auth.user
    if form.validate():
        try:
            new_component = new_node(
                title=strip_html(form.title.data),
                user=user,
                category=form.category.data,
                parent=node,
            )
        except ValidationValueError as e:
            raise HTTPError(
                http.BAD_REQUEST,
                data=dict(message_long=e.message)
            )
        redirect_url = node.url
        message = (
            'Your component was created successfully. You can keep working on the project page below, '
            'or go to the new <u><a href={component_url}>component</a></u>.'
        ).format(component_url=new_component.url)
        if form.inherit_contributors.data and node.has_permission(user, WRITE):
            for contributor in node.contributors:
                perm = CREATOR_PERMISSIONS if contributor is user else node.get_permissions(contributor)
                new_component.add_contributor(contributor, permissions=perm, auth=auth)

            new_component.save()
            redirect_url = new_component.url + 'contributors/'
            message = (
                'Your component was created successfully. You can edit the contributor permissions below, '
                'work on your <u><a href={component_url}>component</a></u> or return to the <u> '
                '<a href="{project_url}">project page</a></u>.'
            ).format(component_url=new_component.url, project_url=node.url)
        status.push_status_message(message, kind='info', trust=True)

        return {
            'status': 'success',
        }, 201, None, redirect_url
    else:
        # TODO: This function doesn't seem to exist anymore?
        status.push_errors_to_status(form.errors)
    raise HTTPError(http.BAD_REQUEST, redirect_url=node.url)


@must_be_logged_in
@must_be_valid_project
def project_before_fork(auth, node, **kwargs):
    user = auth.user

    prompts = node.callback('before_fork', user=user)

    if node.has_pointers_recursive:
        prompts.append(
            language.BEFORE_FORK_HAS_POINTERS.format(
                category=node.project_or_component
            )
        )

    return {'prompts': prompts}


@must_be_logged_in
@must_be_valid_project
def project_before_template(auth, node, **kwargs):
    prompts = []

    for addon in node.get_addons():
        if 'node' in addon.config.configs:
            if addon.to_json(auth.user)['addon_full_name']:
                prompts.append(addon.to_json(auth.user)['addon_full_name'])

    return {'prompts': prompts}


@must_be_logged_in
@must_be_valid_project
@http_error_if_disk_saving_mode
def node_fork_page(auth, node, **kwargs):
    try:
        fork = node.fork_node(auth)
    except PermissionsError:
        raise HTTPError(
            http.FORBIDDEN,
            redirect_url=node.url
        )
    message = '{} has been successfully forked.'.format(
        node.project_or_component.capitalize()
    )
    status.push_status_message(message, kind='success', trust=False)
    return fork.url


@must_be_valid_project
@must_be_contributor_or_public_but_not_anonymized
def node_registrations(auth, node, **kwargs):
    return _view_project(node, auth, primary=True)


@must_be_valid_project
@must_be_contributor_or_public_but_not_anonymized
def node_forks(auth, node, **kwargs):
    return _view_project(node, auth, primary=True)


@must_be_valid_project
@must_be_logged_in
@must_have_permission(READ)
def node_setting(auth, node, **kwargs):

    #check institutions:
    try:
        email_domains = [email.split('@')[1] for email in auth.user.emails]
        inst = Institution.find_one(Q('email_domains', 'in', email_domains))
        if inst not in auth.user.affiliated_institutions:
            auth.user.affiliated_institutions.append(inst)
            auth.user.save()
    except (IndexError, NoResultsFound):
        pass

    ret = _view_project(node, auth, primary=True)

    addons_enabled = []
    addon_enabled_settings = []

    for addon in node.get_addons():
        addons_enabled.append(addon.config.short_name)
        if 'node' in addon.config.configs:
            config = addon.to_json(auth.user)
            # inject the MakoTemplateLookup into the template context
            # TODO inject only short_name and render fully client side
            config['template_lookup'] = addon.config.template_lookup
            config['addon_icon_url'] = addon.config.icon_url
            addon_enabled_settings.append(config)

    addon_enabled_settings = sorted(addon_enabled_settings, key=lambda addon: addon['addon_full_name'].lower())

    ret['addon_categories'] = settings.ADDON_CATEGORIES
    ret['addons_available'] = sorted([
        addon
        for addon in settings.ADDONS_AVAILABLE
        if 'node' in addon.owners
        and addon.short_name not in settings.SYSTEM_ADDED_ADDONS['node'] and addon.short_name != 'wiki'
    ], key=lambda addon: addon.full_name.lower())

    for addon in settings.ADDONS_AVAILABLE:
        if 'node' in addon.owners and addon.short_name not in settings.SYSTEM_ADDED_ADDONS['node'] and addon.short_name == 'wiki':
            ret['wiki'] = addon
            break

    ret['addons_enabled'] = addons_enabled
    ret['addon_enabled_settings'] = addon_enabled_settings
    ret['addon_capabilities'] = settings.ADDON_CAPABILITIES
    ret['addon_js'] = collect_node_config_js(node.get_addons())

    ret['include_wiki_settings'] = node.include_wiki_settings(auth.user)

    ret['comments'] = {
        'level': node.comment_level,
    }

    ret['categories'] = Node.CATEGORY_MAP
    ret['categories'].update({
        'project': 'Project'
    })

    return ret
def collect_node_config_js(addons):
    """Collect webpack bundles for each of the addons' node-cfg.js modules. Return
    the URLs for each of the JS modules to be included on the node addons config page.

    :param list addons: List of node's addon config records.
    """
    js_modules = []
    for addon in addons:
        js_path = paths.resolve_addon_path(addon.config, 'node-cfg.js')
        if js_path:
            js_modules.append(js_path)
    return js_modules


@must_have_permission(WRITE)
@must_not_be_registration
def node_choose_addons(auth, node, **kwargs):
    node.config_addons(request.json, auth)


@must_be_valid_project
@must_have_permission(READ)
def node_contributors(auth, node, **kwargs):
    ret = _view_project(node, auth, primary=True)
    ret['contributors'] = utils.serialize_contributors(node.contributors, node)
    ret['adminContributors'] = utils.serialize_contributors(node.admin_contributors, node, admin=True)
    return ret


@must_have_permission(ADMIN)
def configure_comments(node, **kwargs):
    comment_level = request.json.get('commentLevel')
    if not comment_level:
        node.comment_level = None
    elif comment_level in ['public', 'private']:
        node.comment_level = comment_level
    else:
        raise HTTPError(http.BAD_REQUEST)
    node.save()


##############################################################################
# View Project
##############################################################################

@must_be_valid_project(retractions_valid=True)
@must_be_contributor_or_public
@process_token_or_pass
def view_project(auth, node, **kwargs):
    primary = '/api/v1' not in request.path
    ret = _view_project(node, auth, primary=primary)

    ret['addon_capabilities'] = settings.ADDON_CAPABILITIES
    # Collect the URIs to the static assets for addons that have widgets
    ret['addon_widget_js'] = list(collect_addon_js(
        node,
        filename='widget-cfg.js',
        config_entry='widget'
    ))
    ret.update(rubeus.collect_addon_assets(node))
    return ret

# Reorder components
@must_be_valid_project
@must_not_be_registration
@must_have_permission(WRITE)
def project_reorder_components(node, **kwargs):
    """Reorders the components in a project's component list.

    :param-json list new_list: List of strings that include node IDs and
        node type delimited by ':'.

    """
    # TODO(sloria): Change new_list parameter to be an array of objects
    # {
    #   'newList': {
    #       {'key': 'abc123', 'type': 'node'}
    #   }
    # }
    new_list = [
        tuple(n.split(':'))
        for n in request.json.get('new_list', [])
    ]
    nodes_new = [
        StoredObject.get_collection(schema).load(key)
        for key, schema in new_list
    ]

    valid_nodes = [
        n for n in node.nodes
        if not n.is_deleted
    ]
    deleted_nodes = [
        n for n in node.nodes
        if n.is_deleted
    ]
    if len(valid_nodes) == len(nodes_new) and set(valid_nodes) == set(nodes_new):
        node.nodes = nodes_new + deleted_nodes
        node.save()
        return {}

    logger.error('Got invalid node list in reorder components')
    raise HTTPError(http.BAD_REQUEST)


##############################################################################


@must_be_valid_project
@must_be_contributor_or_public
def project_statistics(auth, node, **kwargs):
    if not (node.can_edit(auth) or node.is_public):
        raise HTTPError(http.FORBIDDEN)
    return _view_project(node, auth, primary=True)


@must_be_valid_project
@must_be_contributor_or_public
def project_statistics_redirect(auth, node, **kwargs):
    return redirect(node.web_url_for("project_statistics", _guid=True))

###############################################################################
# Make Private/Public
###############################################################################


@must_be_valid_project
@must_have_permission(ADMIN)
def project_before_set_public(node, **kwargs):
    prompt = node.callback('before_make_public')

    return {
        'prompts': prompt
    }


@must_be_valid_project
@must_have_permission(ADMIN)
def project_set_privacy(auth, node, **kwargs):

    permissions = kwargs.get('permissions')
    if permissions is None:
        raise HTTPError(http.BAD_REQUEST)

    try:
        node.set_privacy(permissions, auth)
    except NodeStateError as e:
        raise HTTPError(http.BAD_REQUEST, data=dict(
            message_short="Can't change privacy",
            message_long=e.message
        ))

    return {
        'status': 'success',
        'permissions': permissions,
    }


@must_be_valid_project
@must_be_contributor_or_public
@must_not_be_registration
def watch_post(auth, node, **kwargs):
    user = auth.user
    watch_config = WatchConfig(node=node,
                               digest=request.json.get('digest', False),
                               immediate=request.json.get('immediate', False))
    try:
        user.watch(watch_config)
    except ValueError:  # Node is already being watched
        raise HTTPError(http.BAD_REQUEST)

    user.save()

    return {
        'status': 'success',
        'watchCount': node.watches.count()
    }


@must_be_valid_project
@must_be_contributor_or_public
@must_not_be_registration
def unwatch_post(auth, node, **kwargs):
    user = auth.user
    watch_config = WatchConfig(node=node,
                               digest=request.json.get('digest', False),
                               immediate=request.json.get('immediate', False))
    try:
        user.unwatch(watch_config)
    except ValueError:  # Node isn't being watched
        raise HTTPError(http.BAD_REQUEST)

    return {
        'status': 'success',
        'watchCount': node.watches.count()
    }


@must_be_valid_project
@must_be_contributor_or_public
@must_not_be_registration
def togglewatch_post(auth, node, **kwargs):
    '''View for toggling watch mode for a node.'''
    # TODO: refactor this, watch_post, unwatch_post (@mambocab)
    user = auth.user
    watch_config = WatchConfig(
        node=node,
        digest=request.json.get('digest', False),
        immediate=request.json.get('immediate', False)
    )
    try:
        if user.is_watching(node):
            user.unwatch(watch_config)
        else:
            user.watch(watch_config)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    user.save()

    return {
        'status': 'success',
        'watchCount': node.watches.count(),
        'watched': user.is_watching(node)
    }

@must_be_valid_project
@must_not_be_registration
@must_have_permission(WRITE)
def update_node(auth, node, **kwargs):
    # in node.update() method there is a key list node.WRITABLE_WHITELIST only allow user to modify
    # category, title, and discription which can be edited by write permission contributor
    data = r_strip_html(request.get_json())
    try:
        updated_field_names = node.update(data, auth=auth)
    except NodeUpdateError as e:
        raise HTTPError(400, data=dict(
            message_short="Failed to update attribute '{0}'".format(e.key),
            message_long=e.reason
        ))
    # Need to cast tags to a string to make them JSON-serialiable
    updated_fields_dict = {
        key: getattr(node, key) if key != 'tags' else [str(tag) for tag in node.tags]
        for key in updated_field_names
        if key != 'logs' and key != 'date_modified'
    }
    node.save()
    return {'updated_fields': updated_fields_dict}


@must_be_valid_project
@must_have_permission(ADMIN)
@must_not_be_registration
def component_remove(auth, node, **kwargs):
    """Remove component, and recursively remove its children. If node has a
    parent, add log and redirect to parent; else redirect to user dashboard.

    """
    try:
        node.remove_node(auth)
    except NodeStateError as e:
        raise HTTPError(
            http.BAD_REQUEST,
            data={
                'message_short': 'Error',
                'message_long': 'Could not delete component: ' + e.message
            },
        )
    node.save()

    message = '{} has been successfully deleted.'.format(
        node.project_or_component.capitalize()
    )
    status.push_status_message(message, kind='success', trust=False)
    parent = node.parent_node
    if parent and parent.can_view(auth):
        redirect_url = node.node__parent[0].url
    else:
        redirect_url = '/dashboard/'

    return {
        'url': redirect_url,
    }


@must_be_valid_project
@must_have_permission(ADMIN)
def remove_private_link(*args, **kwargs):
    link_id = request.json['private_link_id']

    try:
        link = PrivateLink.load(link_id)
        link.is_deleted = True
        link.save()
    except ModularOdmException:
        raise HTTPError(http.NOT_FOUND)


# TODO: Split into separate functions
def _render_addon(node):

    widgets = {}
    configs = {}
    js = []
    css = []

    for addon in node.get_addons():
        configs[addon.config.short_name] = addon.config.to_json()
        js.extend(addon.config.include_js.get('widget', []))
        css.extend(addon.config.include_css.get('widget', []))

        js.extend(addon.config.include_js.get('files', []))
        css.extend(addon.config.include_css.get('files', []))

    return widgets, configs, js, css


def _should_show_wiki_widget(node, user):

    has_wiki = bool(node.get_addon('wiki'))
    wiki_page = node.get_wiki_page('home', None)
    if not node.has_permission(user, 'write'):
        return has_wiki and wiki_page and wiki_page.html(node)
    else:
        return has_wiki


def _view_project(node, auth, primary=False):
    """Build a JSON object containing everything needed to render
    project.view.mako.
    """
    user = auth.user

    parent = node.parent_node
    if user:
        bookmark_collection = find_bookmark_collection(user)
        bookmark_collection_id = bookmark_collection._id
        in_bookmark_collection = bookmark_collection.pointing_at(node._primary_key) is not None
    else:
        in_bookmark_collection = False
        bookmark_collection_id = ''
    view_only_link = auth.private_key or request.args.get('view_only', '').strip('/')
    anonymous = has_anonymous_link(node, auth)
    widgets, configs, js, css = _render_addon(node)
    redirect_url = node.url + '?view_only=None'

    disapproval_link = ''
    if (node.is_pending_registration and node.has_permission(user, ADMIN)):
        disapproval_link = node.root.registration_approval.stashed_urls.get(user._id, {}).get('reject', '')

    # Before page load callback; skip if not primary call
    if primary:
        for addon in node.get_addons():
            messages = addon.before_page_load(node, user) or []
            for message in messages:
                status.push_status_message(message, kind='info', dismissible=False, trust=True)
    data = {
        'node': {
            'disapproval_link': disapproval_link,
            'id': node._primary_key,
            'title': node.title,
            'category': node.category_display,
            'category_short': node.category,
            'node_type': node.project_or_component,
            'description': node.description or '',
            'license': serialize_node_license_record(node.license),
            'url': node.url,
            'api_url': node.api_url,
            'absolute_url': node.absolute_url,
            'redirect_url': redirect_url,
            'display_absolute_url': node.display_absolute_url,
            'update_url': node.api_url_for('update_node'),
            'in_dashboard': in_bookmark_collection,
            'is_public': node.is_public,
            'is_archiving': node.archiving,
            'date_created': iso8601format(node.date_created),
            'date_modified': iso8601format(node.logs[-1].date) if node.logs else '',
            'tags': [tag._primary_key for tag in node.tags],
            'children': bool(node.nodes_active),
            'is_registration': node.is_registration,
            'is_pending_registration': node.is_pending_registration,
            'is_retracted': node.is_retracted,
            'is_pending_retraction': node.is_pending_retraction,
            'retracted_justification': getattr(node.retraction, 'justification', None),
            'embargo_end_date': node.embargo_end_date.strftime("%A, %b. %d, %Y") if node.embargo_end_date else False,
            'is_pending_embargo': node.is_pending_embargo,
            'is_embargoed': node.is_embargoed,
            'is_pending_embargo_termination': node.is_embargoed and (
                node.embargo_termination_approval and
                node.embargo_termination_approval.is_pending_approval
            ),
            'registered_from_url': node.registered_from.url if node.is_registration else '',
            'registered_date': iso8601format(node.registered_date) if node.is_registration else '',
            'root_id': node.root._id if node.root else None,
            'registered_meta': node.registered_meta,
            'registered_schemas': serialize_meta_schemas(node.registered_schema),
            'registration_count': node.registrations_all.count(),
            'is_fork': node.is_fork,
            'forked_from_id': node.forked_from._primary_key if node.is_fork else '',
            'forked_from_display_absolute_url': node.forked_from.display_absolute_url if node.is_fork else '',
            'forked_date': iso8601format(node.forked_date) if node.is_fork else '',
            'fork_count': node.forks.count(),
            'templated_count': node.templated_list.count(),
            'watched_count': node.watches.count(),
            'private_links': [x.to_json() for x in node.private_links_active],
            'link': view_only_link,
            'anonymous': anonymous,
            'points': len(node.get_points(deleted=False, folders=False)),
            'piwik_site_id': node.piwik_site_id,
            'comment_level': node.comment_level,
            'has_comments': bool(Comment.find(Q('node', 'eq', node))),
            'has_children': bool(Comment.find(Q('node', 'eq', node))),
            'identifiers': {
                'doi': node.get_identifier_value('doi'),
                'ark': node.get_identifier_value('ark'),
            },
            'institution': {
                'name': node.primary_institution.name if node.primary_institution else None,
                'logo_path': node.primary_institution.logo_path if node.primary_institution else None,
                'id': node.primary_institution._id if node.primary_institution else None
            },
            'alternative_citations': [citation.to_json() for citation in node.alternative_citations],
            'has_draft_registrations': node.has_active_draft_registrations,
            'contributors': [contributor._id for contributor in node.contributors]
        },
        'parent_node': {
            'exists': parent is not None,
            'id': parent._primary_key if parent else '',
            'title': parent.title if parent else '',
            'category': parent.category_display if parent else '',
            'url': parent.url if parent else '',
            'api_url': parent.api_url if parent else '',
            'absolute_url': parent.absolute_url if parent else '',
            'registrations_url': parent.web_url_for('node_registrations') if parent else '',
            'is_public': parent.is_public if parent else '',
            'is_contributor': parent.is_contributor(user) if parent else '',
            'can_view': parent.can_view(auth) if parent else False
        },
        'user': {
            'is_contributor': node.is_contributor(user),
            'is_admin': node.has_permission(user, ADMIN),
            'is_admin_parent': parent.is_admin_parent(user) if parent else False,
            'can_edit': (node.can_edit(auth)
                         and not node.is_registration),
            'has_read_permissions': node.has_permission(user, READ),
            'permissions': node.get_permissions(user) if user else [],
            'is_watching': user.is_watching(node) if user else False,
            'piwik_token': user.piwik_token if user else '',
            'id': user._id if user else None,
            'username': user.username if user else None,
            'fullname': user.fullname if user else '',
            'can_comment': node.can_comment(auth),
            'show_wiki_widget': _should_show_wiki_widget(node, user),
            'dashboard_id': bookmark_collection_id,
            'institutions': get_affiliated_institutions(user) if user else [],
        },
        'badges': _get_badge(user),
        # TODO: Namespace with nested dicts
        'addons_enabled': node.get_addon_names(),
        'addons': configs,
        'addon_widgets': widgets,
        'addon_widget_js': js,
        'addon_widget_css': css,
        'node_categories': Node.CATEGORY_MAP
    }
    return data

def get_affiliated_institutions(obj):
    ret = []
    for institution in obj.affiliated_institutions:
        ret.append({
            'name': institution.name,
            'logo_path': institution.logo_path,
            'id': institution._id,
        })
    return ret

def _get_badge(user):
    if user:
        badger = user.get_addon('badges')
        if badger:
            return {
                'can_award': badger.can_award,
                'badges': badger.get_badges_json()
            }
    return {}


def _get_children(node, auth, indent=0):

    children = []

    for child in node.nodes_primary:
        if not child.is_deleted and child.has_permission(auth.user, ADMIN):
            children.append({
                'id': child._primary_key,
                'title': child.title,
                'indent': indent,
                'is_public': child.is_public,
                'parent_id': child.parent_id,
            })
            children.extend(_get_children(child, auth, indent + 1))

    return children


@must_be_valid_project
@must_have_permission(ADMIN)
def private_link_table(node, **kwargs):
    data = {
        'node': {
            'absolute_url': node.absolute_url,
            'private_links': [x.to_json() for x in node.private_links_active],
        }
    }
    return data


@collect_auth
@must_be_valid_project
@must_have_permission(ADMIN)
def get_editable_children(auth, node, **kwargs):

    children = _get_children(node, auth)

    return {
        'node': {'id': node._id, 'title': node.title, 'is_public': node.is_public},
        'children': children,
    }


@must_be_valid_project
def get_recent_logs(node, **kwargs):
    logs = list(reversed(node.logs._to_primary_keys()))[:3]
    return {'logs': logs}


def _get_summary(node, auth, primary=True, link_id=None, show_path=False):
    # TODO(sloria): Refactor this or remove (lots of duplication with _view_project)
    summary = {
        'id': link_id if link_id else node._id,
        'primary': primary,
        'is_registration': node.is_registration,
        'is_fork': node.is_fork,
        'is_pending_registration': node.is_pending_registration,
        'is_retracted': node.is_retracted,
        'is_pending_retraction': node.is_pending_retraction,
        'embargo_end_date': node.embargo_end_date.strftime("%A, %b. %d, %Y") if node.embargo_end_date else False,
        'is_pending_embargo': node.is_pending_embargo,
        'is_embargoed': node.is_embargoed,
        'archiving': node.archiving,
    }

    if node.can_view(auth):
        summary.update({
            'can_view': True,
            'can_edit': node.can_edit(auth),
            'primary_id': node._id,
            'url': node.url,
            'primary': primary,
            'api_url': node.api_url,
            'title': node.title,
            'category': node.category,
            'node_type': node.project_or_component,
            'is_fork': node.is_fork,
            'is_registration': node.is_registration,
            'anonymous': has_anonymous_link(node, auth),
            'registered_date': node.registered_date.strftime('%Y-%m-%d %H:%M UTC')
            if node.is_registration
            else None,
            'forked_date': node.forked_date.strftime('%Y-%m-%d %H:%M UTC')
            if node.is_fork
            else None,
            'ua_count': None,
            'ua': None,
            'non_ua': None,
            'addons_enabled': node.get_addon_names(),
            'is_public': node.is_public,
            'parent_title': node.parent_node.title if node.parent_node else None,
            'parent_is_public': node.parent_node.is_public if node.parent_node else False,
            'show_path': show_path,
            'nlogs': len(node.logs),
        })
    else:
        summary['can_view'] = False

    # TODO: Make output format consistent with _view_project
    return {
        'summary': summary,
    }


@collect_auth
@must_be_valid_project(retractions_valid=True)
def get_summary(auth, node, **kwargs):
    primary = kwargs.get('primary')
    link_id = kwargs.get('link_id')
    show_path = kwargs.get('show_path', False)

    return _get_summary(
        node, auth, primary=primary, link_id=link_id, show_path=show_path
    )


@must_be_contributor_or_public
def get_children(auth, node, **kwargs):
    user = auth.user
    if request.args.get('permissions'):
        perm = request.args['permissions'].lower().strip()
        nodes = [
            each
            for each in node.nodes
            if perm in each.get_permissions(user) and not each.is_deleted
        ]
    else:
        nodes = [
            each
            for each in node.nodes
            if not each.is_deleted
        ]
    return _render_nodes(nodes, auth)


def node_child_tree(user, node_ids):
    """ Format data to test for node privacy settings for use in treebeard.
    """
    items = []
    for node_id in node_ids:
        node = Node.load(node_id)
        assert node, '{} is not a valid Node.'.format(node_id)

        can_read = node.has_permission(user, 'read')
        can_read_children = node.has_permission_on_children(user, 'read')
        if not can_read and not can_read_children:
            continue

        contributors = []
        for contributor in node.contributors:
            contributors.append({
                'id': contributor._id,
                'is_admin': node.has_permission(contributor, ADMIN),
                'is_confirmed': contributor.is_confirmed
            })

        children = []
        # List project/node if user has at least 'read' permissions (contributor or admin viewer) or if
        # user is contributor on a component of the project/node
        can_write = node.has_permission(user, 'admin')
        children.extend(node_child_tree(
            user,
            [
                n._id
                for n in node.nodes
                if n.primary and
                not n.is_deleted
            ]
        ))
        item = {
            'node': {
                'id': node_id,
                'url': node.url if can_read else '',
                'title': node.title if can_read else 'Private Project',
                'is_public': node.is_public,
                'can_write': can_write,
                'contributors': contributors,
                'visible_contributors': node.visible_contributor_ids,
                'is_admin': node.has_permission(user, ADMIN)
            },
            'user_id': user._id,
            'children': children,
            'kind': 'folder' if not node.node__parent or not node.parent_node.has_permission(user, 'read') else 'node',
            'nodeType': node.project_or_component,
            'category': node.category,
            'permissions': {
                'view': can_read,
            }
        }

        items.append(item)

    return items


@must_be_logged_in
@must_be_valid_project
def get_node_tree(auth, **kwargs):
    node = kwargs.get('node') or kwargs['project']
    tree = node_child_tree(auth.user, [node._id])
    return tree

@must_be_contributor_or_public
def get_forks(auth, node, **kwargs):
    fork_list = node.forks.sort('-forked_date')
    return _render_nodes(nodes=fork_list, auth=auth)


@must_be_contributor_or_public
def get_registrations(auth, node, **kwargs):
    registrations = [n for n in reversed(node.registrations_all) if not n.is_deleted]  # get all registrations, including archiving
    return _render_nodes(registrations, auth)


@must_be_valid_project
@must_have_permission(ADMIN)
def project_generate_private_link_post(auth, node, **kwargs):
    """ creata a new private link object and add it to the node and its selected children"""

    node_ids = request.json.get('node_ids', [])
    name = request.json.get('name', '')

    anonymous = request.json.get('anonymous', False)

    if node._id not in node_ids:
        node_ids.insert(0, node._id)

    nodes = [Node.load(node_id) for node_id in node_ids]

    try:
        new_link = new_private_link(
            name=name, user=auth.user, nodes=nodes, anonymous=anonymous
        )
    except ValidationValueError as e:
        raise HTTPError(
            http.BAD_REQUEST,
            data=dict(message_long=e.message)
        )

    return new_link


@must_be_valid_project
@must_have_permission(ADMIN)
def project_private_link_edit(auth, **kwargs):
    name = request.json.get('value', '')
    try:
        validate_title(name)
    except ValidationValueError as e:
        message = 'Invalid link name.' if e.message == 'Invalid title.' else e.message
        raise HTTPError(
            http.BAD_REQUEST,
            data=dict(message_long=message)
        )

    private_link_id = request.json.get('pk', '')
    private_link = PrivateLink.load(private_link_id)

    if private_link:
        new_name = strip_html(name)
        private_link.name = new_name
        private_link.save()
        return new_name
    else:
        raise HTTPError(
            http.BAD_REQUEST,
            data=dict(message_long='View-only link not found.')
        )


def _serialize_node_search(node):
    """Serialize a node for use in pointer search.

    :param Node node: Node to serialize
    :return: Dictionary of node data

    """
    title = node.title
    if node.is_registration:
        title += ' (registration)'

    first_author = node.visible_contributors[0]

    return {
        'id': node._id,
        'title': title,
        'firstAuthor': first_author.family_name or first_author.given_name or first_author.full_name,
        'etal': len(node.visible_contributors) > 1,
    }


@must_be_logged_in
def search_node(auth, **kwargs):
    """

    """
    # Get arguments
    node = Node.load(request.json.get('nodeId'))
    include_public = request.json.get('includePublic')
    size = float(request.json.get('size', '5').strip())
    page = request.json.get('page', 0)
    query = request.json.get('query', '').strip()

    start = (page * size)
    if not query:
        return {'nodes': []}

    # Build ODM query
    title_query = Q('title', 'icontains', query)
    not_deleted_query = Q('is_deleted', 'eq', False)
    visibility_query = Q('contributors', 'eq', auth.user)
    no_folders_query = Q('is_collection', 'eq', False)
    if include_public:
        visibility_query = visibility_query | Q('is_public', 'eq', True)
    odm_query = title_query & not_deleted_query & visibility_query & no_folders_query

    # Exclude current node from query if provided
    if node:
        nin = [node._id] + node.node_ids
        odm_query = (
            odm_query &
            Q('_id', 'nin', nin)
        )

    nodes = Node.find(odm_query)
    count = nodes.count()
    pages = math.ceil(count / size)
    validate_page_num(page, pages)

    return {
        'nodes': [
            _serialize_node_search(each)
            for each in islice(nodes, start, start + size)
            if each.contributors
        ],
        'total': count,
        'pages': pages,
        'page': page
    }


def _add_pointers(node, pointers, auth):
    """

    :param Node node: Node to which pointers will be added
    :param list pointers: Nodes to add as pointers

    """
    added = False
    for pointer in pointers:
        node.add_pointer(pointer, auth, save=False)
        added = True

    if added:
        node.save()


@collect_auth
def move_pointers(auth):
    """Move pointer from one node to another node.

    """

    from_node_id = request.json.get('fromNodeId')
    to_node_id = request.json.get('toNodeId')
    pointers_to_move = request.json.get('pointerIds')

    if from_node_id is None or to_node_id is None or pointers_to_move is None:
        raise HTTPError(http.BAD_REQUEST)

    from_node = Node.load(from_node_id)
    to_node = Node.load(to_node_id)

    if to_node is None or from_node is None:
        raise HTTPError(http.BAD_REQUEST)

    for pointer_to_move in pointers_to_move:
        pointer_id = from_node.pointing_at(pointer_to_move)
        pointer_node = Node.load(pointer_to_move)

        pointer = Pointer.load(pointer_id)
        if pointer is None:
            raise HTTPError(http.BAD_REQUEST)

        try:
            from_node.rm_pointer(pointer, auth=auth)
        except ValueError:
            raise HTTPError(http.BAD_REQUEST)

        from_node.save()
        try:
            _add_pointers(to_node, [pointer_node], auth)
        except ValueError:
            raise HTTPError(http.BAD_REQUEST)

    return {}, 200, None


@collect_auth
def add_pointer(auth):
    """Add a single pointer to a node using only JSON parameters

    """
    to_node_id = request.json.get('toNodeID')
    pointer_to_move = request.json.get('pointerID')

    if not (to_node_id and pointer_to_move):
        raise HTTPError(http.BAD_REQUEST)

    pointer = Node.load(pointer_to_move)
    to_node = Node.load(to_node_id)
    try:
        _add_pointers(to_node, [pointer], auth)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)


@must_have_permission(WRITE)
@must_not_be_registration
def add_pointers(auth, node, **kwargs):
    """Add pointers to a node.

    """
    node_ids = request.json.get('nodeIds')

    if not node_ids:
        raise HTTPError(http.BAD_REQUEST)

    nodes = [
        Node.load(node_id)
        for node_id in node_ids
    ]

    try:
        _add_pointers(node, nodes, auth)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    return {}


@must_have_permission(WRITE)
@must_not_be_registration
def remove_pointer(auth, node, **kwargs):
    """Remove a pointer from a node, raising a 400 if the pointer is not
    in `node.nodes`.

    """
    # TODO: since these a delete request, shouldn't use request body. put pointer
    # id in the URL instead
    pointer_id = request.json.get('pointerId')
    if pointer_id is None:
        raise HTTPError(http.BAD_REQUEST)

    pointer = Pointer.load(pointer_id)
    if pointer is None:
        raise HTTPError(http.BAD_REQUEST)

    try:
        node.rm_pointer(pointer, auth=auth)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    node.save()


@must_be_valid_project  # injects project
@must_have_permission(WRITE)
@must_not_be_registration
def remove_pointer_from_folder(auth, node, pointer_id, **kwargs):
    """Remove a pointer from a node, raising a 400 if the pointer is not
    in `node.nodes`.

    """
    if pointer_id is None:
        raise HTTPError(http.BAD_REQUEST)

    pointer_id = node.pointing_at(pointer_id)

    pointer = Pointer.load(pointer_id)

    if pointer is None:
        raise HTTPError(http.BAD_REQUEST)

    try:
        node.rm_pointer(pointer, auth=auth)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    node.save()


@must_have_permission(WRITE)
@must_not_be_registration
def fork_pointer(auth, node, **kwargs):
    """Fork a pointer. Raises BAD_REQUEST if pointer not provided, not found,
    or not present in `nodes`.

    """
    pointer_id = request.json.get('pointerId')
    pointer = Pointer.load(pointer_id)

    if pointer is None:
        # TODO: Change this to 404?
        raise HTTPError(http.BAD_REQUEST)

    try:
        node.fork_pointer(pointer, auth=auth, save=True)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)


def abbrev_authors(node):
    lead_author = node.visible_contributors[0]
    ret = lead_author.family_name or lead_author.given_name or lead_author.fullname
    if len(node.visible_contributor_ids) > 1:
        ret += ' et al.'
    return ret


def serialize_pointer(pointer, auth):
    node = get_pointer_parent(pointer)

    if node.can_view(auth):
        return {
            'id': node._id,
            'url': node.url,
            'title': node.title,
            'authorShort': abbrev_authors(node),
        }
    return {
        'url': None,
        'title': 'Private Component',
        'authorShort': 'Private Author(s)',
    }


@must_be_contributor_or_public
def get_pointed(auth, node, **kwargs):
    """View that returns the pointers for a project."""
    # exclude folders
    return {'pointed': [
        serialize_pointer(each, auth)
        for each in node.pointed
        if not get_pointer_parent(each).is_collection
    ]}
