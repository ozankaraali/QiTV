-- QiTV lifecycle/navigation additions; uosc supplies the managed interface.
local mp = require 'mp'
local owner = nil
local started_at = mp.get_time()
local pip = nil

local function notify(action)
    if owner then
        return mp.commandv('script-message-to', owner, 'qitv', action)
    end
end

mp.register_script_message('hello', function(client)
    owner = client
    notify('ready')
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
