import logging
from urllib.parse import quote

import aiohttp
import slacker

from opsdroid.matchers import match_regex
from opsdroid.matchers import match_crontab
from opsdroid.message import Message

from matrix_client.errors import MatrixRequestError

_LOGGER = logging.getLogger(__name__)


def get_room_members(slack, channel_id):
    """
    Get a list of members in a given room
    """
    resp = slack.channels.get("conversations.members", params={'channel': channel_id})
    return resp.body['members']


def join_bot_to_channel(bot_slack, config, bot_id, channel_id):
    """
    Invite the bot to the channel if the bot is not already in the channel.
    """
    u_token = config['slack_user_token']
    slack = slacker.Slacker(u_token)
    members = get_room_members(bot_slack, channel_id)
    if bot_id not in members:
        # Do an extra guard here just in case
        try:
            slack.channels.invite(channel_id, bot_id)
        except slacker.Error:
            _LOGGER.exception("Invite failed")


def get_channel_mapping(slack):
    """
    Map slack channel ids to their names
    """
    response = slack.channels.list()
    channels = response.body['channels']

    return {c['id']: c['name'] for c in channels}


def get_new_channels(slack, config, seen_channels):
    """
    Get channels in the workspace that are not in seen_channels
    """
    # Get channel list
    response = slack.channels.list()
    channels = response.body['channels']

    # Get the new channels we need to process
    new_channels = {}
    for channel in channels:
        if channel['is_archived']:
            continue
        if channel['id'] not in seen_channels.keys():
            prefix = config['room_alias_prefix']
            channel_name = get_channel_mapping(slack)[channel['id']]
            server_name = config['server_name']
            alias = f"#{prefix}{channel_name}:{server_name}"
            topic = channel['topic']['value']
            new_channels[channel['id']] = (channel_name, alias, topic)

    return new_channels


def get_matrix_connector(opsdroid):
    """
    Return the first configured matrix connector.
    """
    for conn in opsdroid.connectors:
        if conn.name == "ConnectorMatrix":
            return conn


async def room_id_if_exists(api, room_alias):
    """
    Returns the room id if the room exists or `None` if it doesn't.
    """
    if room_alias.startswith('!'):
        return room_alias
    try:
        room_id = await api.get_room_id(room_alias)
        return room_id
    except MatrixRequestError as e:
        if e.code != 404:
            raise e
        return None


async def joined_rooms(api):
    respjson = await api._send("GET", "/joined_rooms")
    return respjson['joined_rooms']


async def is_in_matrix_room(api, room_id):
    rooms = await joined_rooms(api)
    return room_id in rooms


async def intent_self_in_room(opsdroid, room):
    """
    This function should result in the connector user being in the given room.
    Irrespective of if that room existed before.
    """

    connector = get_matrix_connector(opsdroid)

    room_id = await room_id_if_exists(connector.connection, room)

    if room_id is None:
        try:
            respjson = await connector.connection.create_room(alias=room.split(':')[0][1:])
            room_id = respjson['room_id']
        except MatrixRequestError:
            room_id = await connector.connection.get_room_id(room)
        respjson = await connector.connection.join_room(room_id)
    else:
        is_in_room = is_in_matrix_room(connector.connection, room_id)

        if not is_in_room:
            respjson = await connector.connection.join_room(room_id)

    return room_id


async def intent_user_in_room(opsdroid, user, room):
    """
    Ensure a user is in a room.

    If the room doesn't exist or the invite fails, then return None
    """
    connector = get_matrix_connector(opsdroid)
    room_id = await room_id_if_exists(connector.connection, room)

    if room_id is not None:
        try:
            await connector.connection.invite_user(room_id, user)
        except MatrixRequestError as e:
            if "already in the room" in e.content:
                return room_id
            room_id = None

    return room_id


async def admin_of_community(opsdroid, community):
    """
    Ensure the community exists, and the user is admin otherwise return None.
    """

    connector = get_matrix_connector(opsdroid)

    # Check the Python SDK speaks communities
    if not hasattr(connector.connection, "create_group"):
        return None

    # Check community exists
    try:
        profile = await connector.connection.get_group_profile(community)
    except MatrixRequestError as e:
        if e.code != 404:
            raise e
        else:
            group = await connector.connection.create_group(community.split(':')[0][1:])
            return group['group_id']

    # Ensure we are admin
    if profile:
        users = await connector.connection.get_users_in_group(community)
        myself = list(filter(lambda key: key['user_id'] == connector.mxid, users['chunk']))
        if not myself[0]['is_privileged']:
            return None

    return community


async def make_community_joinable(opsdroid, community):
    connector = get_matrix_connector(opsdroid)

    content = {"m.join_policy": {"type": "open"}}
    await connector.connection._send("PUT", f"groups/{community}/settings/m.join_policy",
                                     content=content)



"""
Break up all the power level modifications so we only inject one state event
into the room.
"""


async def set_power_levels(opsdroid, room_alias, power_levels):
    connector = get_matrix_connector(opsdroid)
    room_id = await room_id_if_exists(connector.connection, room_alias)
    return await connector.connection.set_power_levels(room_id, power_levels)


async def get_power_levels(opsdroid, room_alias):
    connector = get_matrix_connector(opsdroid)
    room_id = await room_id_if_exists(connector.connection, room_alias)

    return await connector.connection.get_power_levels(room_id)


async def user_is_room_admin(power_levels, room_alias, mxid):
    """
    Modify power_levels so user is admin
    """
    user_pl = power_levels['users'].get(mxid, None)

    # If already admin, skip
    if user_pl != 100:
        power_levels['users'][mxid] = 100

    return power_levels


async def room_notifications_pl0(power_levels, room_alias):
    """
    Set the power levels for @room notifications to 0
    """

    notifications = power_levels.get('notifications', {})
    notifications['room'] = 0

    power_levels['notifications'] = notifications

    return power_levels


async def configure_room_power_levels(opsdroid, config, room_alias):
    """
    Do all the power level related stuff.
    """
    connector = get_matrix_connector(opsdroid)
    room_id = await room_id_if_exists(connector.connection, room_alias)

    # Get the users to be made admin in the matrix room
    users_as_admin = config.get("users_as_admin", [])

    power_levels = await get_power_levels(opsdroid, room_id)

    # Add admin users
    for user in users_as_admin:
        await intent_user_in_room(opsdroid, user, room_id)
        power_levels = await user_is_room_admin(power_levels, room_id, user)

    room_pl_0 = config.get("room_pl_0", False)
    if room_pl_0:
        power_levels = await room_notifications_pl0(power_levels, room_id)

    # Only actually modify room state if we need to
    if users_as_admin or room_pl_0:
        await set_power_levels(opsdroid, room_id, power_levels)


async def get_related_groups(opsdroid, roomid):
    """
    Get the m.room.related_groups state from a room
    """
    connector = get_matrix_connector(opsdroid)
    api = connector.connection

    try:
        json = await api._send("GET", f"/rooms/{roomid}/state/m.room.related_groups")
        return json['groups']
    except MatrixRequestError as e:
        if e.code != 404:
            raise e
        else:
            return []


async def set_related_groups(opsdroid, roomid, communities):
    """
    Set the m.room.related_groups state from a room
    """
    connector = get_matrix_connector(opsdroid)
    api = connector.connection

    content = {'groups': communities}

    return await api.send_state_event(roomid,
                                      "m.room.related_groups",
                                      content)


async def update_related_groups(opsdroid, roomid, communities):
    """
    Add communities to the existing m.room.related_groups state event.
    """

    existing_communities = await get_related_groups(opsdroid, roomid)

    existing_communities += communities

    new_groups = list(set(existing_communities))

    return await set_related_groups(opsdroid, roomid, new_groups)


async def user_in_state(opsdroid, roomid, mxid):
    """
    Check to see if the user exists in the state.
    """
    connector = get_matrix_connector(opsdroid)
    api = connector.connection

    state = await api.get_room_state(roomid)

    keys = [s.get("state_key", "") for s in state]

    return mxid in keys


"""
Helpers for room avatar
"""


async def upload_image_to_matrix(self, image_url):
    """
    Given a URL upload the image to the homeserver for the given user.
    """
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", image_url) as resp:
            data = await resp.read()

    respjson = await self.api.media_upload(data, resp.content_type)

    return respjson['content_uri']


async def set_room_avatar(opsdroid, room_id, avatar_url):
    """
    Set a room avatar.
    """
    connector = get_matrix_connector(opsdroid)

    if not avatar_url.startswith("mxc"):
        avatar_url = await upload_image_to_matrix(avatar_url)

    # Set state event
    content = {
        "url": avatar_url
    }

    return await connector.connection.send_state_event(room_id,
                                                       "m.room.avatar",
                                                       content)


@match_crontab('* * * * *')
@match_regex('!updatechannels')
async def mirror_slack_channels(opsdroid, config, message):
    """
    Check what channels exist in the Slack workspace and list them.
    """

    conn = get_matrix_connector(opsdroid)

    if not message:
        message = Message("",
                          None,
                          config.get("room", conn.default_room),
                          conn)

    token = config['slack_bot_token']
    u_token = config['slack_user_token']
    slack = slacker.Slacker(token)

    # Make public
    make_public = config.get("make_public", True)

    # Get userid for bot user
    bridge_bot_id = config['bridge_bot_name']
    bridge_bot_id = slack.users.get_user_id(bridge_bot_id)

    # Get the channels we have already processed out of memory
    seen_channels = await opsdroid.memory.get("seen_channels")
    seen_channels = seen_channels if seen_channels else {}

    # Get channels that are now in the workspace that we haven't seen before
    new_channels = get_new_channels(slack, config, seen_channels)

    # Ensure that the community exists and we are admin
    # Will return None if we don't have the groups API PR
    community = await admin_of_community(opsdroid, config["community_id"])

    # Get the room name prefix
    room_name_prefix = config.get("room_name_prefix", config["room_alias_prefix"])

    related_groups = config.get("related_groups", [])

    # Get a list of rooms currently in the community
    if community:
        response = await conn.connection.get_rooms_in_group(community)
        rooms_in_community = {r['room_id'] for r in response['chunk']}

    for channel_id, (channel_name, room_alias, topic) in new_channels.items():
        # Apparently this isn't needed
        # Join the slack bot to these new channels
        join_bot_to_channel(slack, config, bridge_bot_id, channel_id)

        # Create a new matrix room for this channels
        room_id = await intent_self_in_room(opsdroid, room_alias)

        # Change the room name to something sane
        room_name = f"{room_name_prefix}{channel_name}"
        await conn.connection.set_room_name(room_id, room_name)
        if topic:
            await conn.connection.set_room_topic(room_id, topic)

        avatar_url = config.get("room_avatar_url", None)
        if avatar_url:
            await set_room_avatar(opsdroid, room_id, avatar_url)

        if make_public:
            # Make room publicly joinable
            try:
                await conn.connection.send_state_event(room_id,
                                                       "m.room.join_rules",
                                                       content={'join_rule': "public"})
                await conn.connection.send_state_event(room_id,
                                                       "m.room.history_visibility",
                                                       content={'history_visibility': "world_readable"})
            except Exception:
                logging.exception("Could not make room publicly joinable")
                await message.respond(f"ERROR: Could not make {room_alias} publically joinable.")

        # Invite the Appservice matrix user to the room
        room_id = await intent_user_in_room(opsdroid, config['as_userid'], room_id)
        if room_id is None:
            await message.respond("ERROR: Could not invite appservice bot"
                                  f"to {room_alias}, skipping channel.")
            continue

        # Make all the changes to room power levels, for both @room and admins
        await configure_room_power_levels(opsdroid, config, room_id)

        # Update related groups
        if related_groups:
            await update_related_groups(opsdroid, room_id, related_groups)

        # Run link command in the appservice admin room
        await message.respond(
            f"link --channel_id {channel_id} --room {room_id}"
            f" --slack_bot_token {token} --slack_user_token {u_token}",
            room='bridge')

        # Invite Users
        if config.get("users_to_invite", None):
            for user in config["users_to_invite"]:
                await intent_user_in_room(opsdroid, user, room_id)

        # Add room to community
        if community and room_id not in rooms_in_community:
            try:
                await conn.connection.add_room_to_group(community, room_id)
            except Exception:
                _LOGGER.exception(f"Failed to add {room_alias} to {community}.")

        if community:
            all_users = await conn.connection.get_users_in_group(community)
            if config.get('invite_communtiy_to_rooms', False):
                for user in all_users['chunk']:
                    # await conn.connection.invite_user(room_id, user['user_id'])
                    in_room = await user_in_state(opsdroid, room_id, user['user_id'])
                    if not in_room:
                        await intent_user_in_room(opsdroid, user['user_id'], room_id)

        await message.respond(f"Finished Adding room {room_alias}")

    if new_channels:
        # update the memory with the channels we just processed
        seen_channels.update(new_channels)
        await opsdroid.memory.put("seen_channels", seen_channels)

        await message.respond(f"Finished adding all rooms.")
