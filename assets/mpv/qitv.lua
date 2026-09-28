-- QiTV lifecycle/navigation additions; uosc supplies the managed interface.
local mp = require 'mp'
local owner = nil
local started_at = mp.get_time()
local pip = nil
local utils = require 'mp.utils'
local timeshift = false
local live_delay = 0
local buffer_state = {}

local function notify(action, ...)
    if owner then
        return mp.commandv('script-message-to', owner, 'qitv', action, ...)
    end
end

mp.register_script_message('hello', function(client)
    owner = client
    notify('ready')
end)

mp.register_script_message('stop', function(entry_id)
    -- A native playlist/Open File choice can overtake QiTV's IPC snapshot.
    -- Stop only the entry QiTV cancelled, not the newer selected media.
    local index = mp.get_property_number('playlist-current-pos')
    local current = index and mp.get_property_number('playlist/' .. index .. '/id')
    if current == tonumber(entry_id) then mp.commandv('stop') end
    notify('stopped', entry_id)
end)
-- Test whether the IPC owner still exists, not whether its UI thread is busy.
-- MPV reports failure when script-message-to targets a disconnected client.
mp.add_periodic_timer(2, function()
    if owner then
        if not notify('alive') then mp.commandv('quit') end
    elseif mp.get_time() - started_at > 12 then
        mp.commandv('quit')
    end
end)

local function restore_pip(restore_fullscreen)
    if not pip then return end
    local saved = pip
    pip = nil
    mp.set_property_native('border', saved.border)
    mp.set_property_native('ontop', saved.ontop)
    mp.set_property_number('current-window-scale', saved.scale)
    if restore_fullscreen ~= false then
        mp.set_property_native('fullscreen', saved.fullscreen)
    end
end

local function toggle_pip()
    if pip then
        restore_pip()
        return
    end
    local scale = mp.get_property_number('current-window-scale')
    if not scale then return end
    pip = {
        scale = scale,
        border = mp.get_property_native('border'),
        ontop = mp.get_property_native('ontop'),
        fullscreen = mp.get_property_native('fullscreen'),
    }
    mp.set_property_native('fullscreen', false)
    mp.set_property_native('border', false)
    mp.set_property_native('ontop', true)
    -- Keep the window's position: MPV has no portable actual-position getter.
    -- Scaling instead of setting geometry lets us restore the pre-PiP size.
    local width = mp.get_property_number('osd-width', 1280)
    mp.set_property_number('current-window-scale', scale * math.min(1, 480 / math.max(1, width)))
end

mp.register_script_message('pip', toggle_pip)
mp.register_script_message('fullscreen', function()
    local fullscreen = not mp.get_property_native('fullscreen')
    if pip then restore_pip(false) end
    mp.set_property_native('fullscreen', fullscreen)
end)
-- Native f/double-click and uosc's fullscreen button must leave PiP as well.
mp.observe_property('fullscreen', 'bool', function(_, fullscreen)
    if fullscreen and pip then restore_pip(false) end
end)
mp.add_key_binding('Alt+p', 'qitv-pip', toggle_pip)
mp.add_key_binding('MBTN_BACK', 'qitv-back', function() notify('back') end)
mp.add_key_binding('MBTN_FORWARD', 'qitv-forward', function() notify('forward') end)
mp.add_key_binding('a', 'qitv-audio', function() mp.commandv('cycle', 'audio') end)
-- QiTV owns resume persistence; never write the user's watch_later directory.
mp.add_key_binding('Q', 'qitv-quit', function() mp.commandv('quit') end)

mp.register_script_message('remote', function(enabled)
    mp.remove_key_binding('qitv-previous')
    mp.remove_key_binding('qitv-next')
    if enabled == 'yes' then
        mp.add_key_binding('UP', 'qitv-previous', function() notify('previous') end, {repeatable = true})
        mp.add_key_binding('DOWN', 'qitv-next', function() notify('next') end, {repeatable = true})
    end
end)

local function timeshift_menu()
    local items = {}
    local title = 'Time-shift'
    if timeshift then
        title = string.format('%.0fs behind live · %.1f / %.0f MiB',
            live_delay, (buffer_state.bytes or 0) / 1048576,
            (buffer_state.max_bytes or 0) / 1048576)
        items = {
            {title = 'Rewind 10 seconds', value = 'script-binding qitv/buffer-back'},
            {title = 'Forward 10 seconds', value = 'script-binding qitv/buffer-forward'},
            {title = 'Go live (1x)', value = 'script-binding qitv/go-live'},
        }
    elseif buffer_state.can_start then
        items = {{title = 'Buffer this stream', value = 'script-binding qitv/buffer-start-recording'}}
    else
        mp.osd_message('Enable Time-shift in QiTV Settings, then play a live stream')
        return
    end
    for _, speed in ipairs({0.5, 0.75, 1, 1.25, 1.5, 2}) do
        items[#items + 1] = {title = speed .. 'x speed', value = 'set speed ' .. speed}
    end
    mp.commandv('script-message-to', 'uosc', 'open-menu', utils.format_json({
        type = 'qitv-timeshift', title = title, items = items,
    }))
end

mp.add_key_binding(nil, 'timeshift-menu', timeshift_menu)
mp.add_key_binding(nil, 'buffer-back', function() notify('buffer-back') end)
mp.add_key_binding(nil, 'buffer-forward', function() notify('buffer-forward') end)
mp.add_key_binding(nil, 'go-live', function()
    if timeshift then notify('buffer-live') else timeshift_menu() end
end)
mp.add_key_binding(nil, 'buffer-start-recording', function() notify('buffer-enable') end)
mp.register_script_message('buffer-seek', function(position, paused)
    if timeshift and tonumber(position) then notify('buffer-seek', position, paused) end
end)

mp.observe_property('user-data/qitv-timeshift', 'native', function(_, state)
    buffer_state = state or {}
    live_delay = math.max(0, (buffer_state['end'] or 0) - (buffer_state.position or 0))
    local active = buffer_state.active == true
    if timeshift == active then return end
    timeshift = active
    for _, name in ipairs({'qitv-rewind', 'qitv-forward-buffer', 'qitv-live', 'qitv-oldest'}) do
        mp.remove_key_binding(name)
    end
    if active then
        mp.add_forced_key_binding('LEFT', 'qitv-rewind',
            function() notify('buffer-back') end, {repeatable = true})
        mp.add_forced_key_binding('RIGHT', 'qitv-forward-buffer',
            function() notify('buffer-forward') end, {repeatable = true})
        mp.add_forced_key_binding('END', 'qitv-live', function() notify('buffer-live') end)
        mp.add_forced_key_binding('HOME', 'qitv-oldest', function() notify('buffer-start') end)
        mp.osd_message('Time-shift: Left/Right ±10s · End Live · speed control to catch up', 5)
    end
end)
