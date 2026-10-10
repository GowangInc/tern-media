-- Session Media is a separate native block; it does not alter Flow or shell state.
local owners = {}
local opening = {}
local pending = false
local mediaExtensions = {
    png = true, jpg = true, jpeg = true, webp = true, gif = true, bmp = true,
    tif = true, tiff = true, heic = true, heif = true, svg = true, avif = true,
    mp4 = true, mov = true, mkv = true, webm = true, m4v = true, avi = true,
    mp3 = true, wav = true, m4a = true, flac = true, ogg = true, aac = true, opus = true, pdf = true,
}

local function gallery(p)
    return p.block == "media.gallery"
end

local function snapshot(pane, callback)
    tern.timer(0, function(cx)
        local panes = cx.session:panes()
        local owner = nil
        for _, p in panes do
            if p.pane == pane then owner = p; break end
        end
        if not owner or not owner.tab then return end
        tern.timer(0, function(fresh)
            local sessions = fresh.session:sessions()
            local session = nil
            for _, s in sessions do
                for _, tab in s.tabs do
                    if tab == owner.tab then session = s; break end
                end
                if session then break end
            end
            if not session then return end
            tern.timer(0, function(reader)
                local hosts = reader.hosts:list()
                local host = nil
                for _, h in hosts do
                    for _, id in h.panes do
                        if id == owner.pane then host = h; break end
                    end
                    if host then break end
                end
                if not host then return end
                local context = {
                    ev = "action", act = "context", pane_id = tostring(owner.pane),
                    window_key = tern.getenv("TERN_WINDOW_KEY") or "",
                    tern_session_id = tostring(session.id), tern_session_name = session.name,
                    host = host.name, cwd = owner.cwd or "", label = owner.title,
                }
                callback(reader, owner, session, host, context, panes)
            end)
        end)
    end)
end

local function findGallery(panes, session)
    for _, p in panes do
        if gallery(p) then
            for _, tab in session.tabs do
                if p.tab == tab then return p end
            end
        end
    end
    return nil
end

local function tell(pane, context)
    tern.timer(0, function(cx)
        if cx.session:pane(pane) then
            tern.timer(0, function(sender) sender.session:event(pane, context) end)
        end
    end)
end

local function open(cx)
    local focused = cx.session:focused()
    if not focused then return end
    snapshot(focused, function(fresh, owner, session, host, context, panes)
        if gallery(owner) then return end
        local existing = findGallery(panes, session)
        if existing then
            owners[existing.pane] = owner.pane
            if existing.tab ~= owner.tab then
                tern.timer(0, function(mover)
                    mover.layout:move(existing.pane, owner.pane, "right", {focus = false})
                end)
            end
            tell(existing.pane, context)
            return
        end
        local key = tostring(host.id) .. ":" .. tostring(session.id)
        if opening[key] then return end
        opening[key] = true
        tern.timer(0, function(creator)
            local id = creator.layout:split(owner.pane, "right", {
                block = "media.gallery", args = {tern.json.encode(context)}, cwd = owner.cwd,
            }, {focus = false})
            opening[key] = nil
            if not id then
                creator:toast("error", "Session Media could not open", "Install this plugin on the pane's host, then reload plugins")
                return
            end
            owners[id] = owner.pane
            tell(id, context)
        end)
    end)
end

local function activeGallery(cx, callback)
    local focused = cx.session:focused()
    if not focused then return end
    snapshot(focused, function(fresh, owner, session, _host, _context, panes)
        local found = gallery(owner) and owner or findGallery(panes, session)
        if found then callback(fresh, found, owners[found.pane] or (not gallery(owner) and owner.pane or nil))
        else fresh:toast("info", "Open Session Media first", "Use Media: Open session gallery") end
    end)
end

local function follow()
    if pending then return end
    pending = true
    tern.timer(0, function(cx)
        local focused = cx.session:focused()
        pending = false
        if not focused then return end
        snapshot(focused, function(_fresh, owner, session, _host, context, panes)
            if gallery(owner) or owner.title == "Flow · Tern" or (owner.kind ~= "terminal" and owner.kind ~= "agent") then return end
            local found = findGallery(panes, session)
            if not found then return end
            owners[found.pane] = owner.pane
            if found.tab ~= owner.tab then
                tern.timer(0, function(mover)
                    mover.layout:move(found.pane, owner.pane, "right", {focus = false})
                end)
            end
            tell(found.pane, context)
        end)
    end)
end

local function callStore(request, callback)
    tern.process.run({"python3", tern.plugin.dir .. "/media-store.py"}, {
        stdin = tern.json.encode(request), timeout_ms = 15000,
    }):next(function(result, err)
        if err or not result or result.status ~= 0 then
            tern.log.warn("Media registration:", err or (result and result.stderr) or "backend unavailable")
            return
        end
        local ok, response = pcall(tern.json.decode, result.stdout)
        if not ok or not response.ok then
            tern.log.warn("Media registration:", ok and response.error or "invalid backend response")
            return
        end
        callback(response)
    end)
end

-- Observe supported native opens without changing Tern's built-in file handling.
tern.route.open(function(req, cx)
    local ext = string.lower(string.match(req.path, "%.([^./]+)$") or "")
    if not mediaExtensions[ext] then return nil end
    local owner = cx.session:focused()
    if not owner then return nil end
    snapshot(owner, function(_fresh, pane, session, host, context)
        if gallery(pane) then return end -- Retained media is already indexed.
        -- A window process must never reinterpret a remote path as a local file.
        if host.kind ~= "local" or (req.host ~= "local" and req.host ~= host.name) then return end
        callStore({op = "list", pane_id = context.pane_id, window_key = context.window_key, limit = 1}, function(response)
            local target = response.session or {
                id = "terminal:" .. context.host .. ":" .. context.window_key .. ":" .. context.pane_id,
                label = pane.title, cwd = pane.cwd or "", host = context.host,
                pane_id = context.pane_id, window_key = context.window_key,
                tern_session_id = tostring(session.id), tern_session_name = session.name,
            }
            callStore({op = "register", session = target, items = {{
                path = req.path, source = req.origin == "drop" and "transferred" or "viewed",
            }}}, function(response)
                if response.errors and #response.errors > 0 then
                    tern.log.warn("Media file registration:", response.errors[1].error)
                end
            end)
        end)
    end)
    return nil
end)

tern.timer(0, function()
    tern.command({id = "open", title = "Media: Open session gallery", group = "Media", run = open})
    tern.command({id = "float", title = "Media: Float gallery", group = "Media", run = function(cx)
        activeGallery(cx, function(fresh, p, owner)
            tern.timer(0, function(mover) mover.layout:float(p.pane, owner, "tr", {focus = false}) end)
        end)
    end})
    tern.command({id = "dock", title = "Media: Dock gallery right", group = "Media", run = function(cx)
        activeGallery(cx, function(fresh, p, owner)
            tern.timer(0, function(mover) mover.layout:dock(p.pane, owner, "right", {focus = false}) end)
        end)
    end})
    tern.command({id = "refresh", title = "Media: Refresh gallery", group = "Media", run = function(cx)
        activeGallery(cx, function(_fresh, p)
            tell(p.pane, {ev = "action", act = "refresh"})
        end)
    end})
    tern.on("focus", follow)
    tern.on("title", follow)
    tern.on("pane_closed", function(ev) owners[ev.pane] = nil end)
end)
