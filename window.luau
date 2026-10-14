-- Session Media is a separate native block; it does not alter Flow or shell state.
--
-- A disabled timer hook drops later callbacks; keep operations out of timer
-- chains. Native block opening and exact CLI ids avoid 0.8.3's large-number
-- round-trip failures in the numeric layout API.
local owners = {}
local closed = {}
local mediaExtensions = {
    png = true, jpg = true, jpeg = true, webp = true, gif = true, bmp = true,
    tif = true, tiff = true, heic = true, heif = true, svg = true, avif = true,
    mp4 = true, mov = true, mkv = true, webm = true, m4v = true, avi = true,
    mp3 = true, wav = true, m4a = true, flac = true, ogg = true, aac = true, opus = true, pdf = true,
}

local function gallery(p)
    return p.block == "media.gallery"
end

-- Resolves the owner pane, its session and its host for pane id `pane`.
-- Returns nil at the first failed hop.
local function snapshot(cx, pane)
    local panes = cx.session:panes()
    local owner = nil
    for _, p in panes do
        if p.pane == pane then owner = p; break end
    end
    if not owner or not owner.tab then return nil end
    local session = nil
    for _, s in cx.session:sessions() do
        for _, tab in s.tabs do
            if tab == owner.tab then session = s; break end
        end
        if session then break end
    end
    if not session then return nil end
    local host = nil
    for _, h in cx.hosts:list() do
        for _, id in h.panes do
            if id == owner.pane then host = h; break end
        end
        if host then break end
    end
    if not host then return nil end
    local context = {
        ev = "action", act = "context", pane_id = tostring(owner.pane),
        window_key = tern.getenv("TERN_WINDOW_KEY") or "",
        tern_session_id = tostring(session.id), tern_session_name = session.name,
        host = host.name, cwd = owner.cwd or "", label = owner.title,
    }
    return owner, session, host, context, panes
end

local function findGallery(panes, session)
    for _, p in panes do
        if gallery(p) and not closed[p.pane] then
            for _, tab in session.tabs do
                if p.tab == tab then return p end
            end
        end
    end
    return nil
end

local function callStore(request, callback)
    tern.process.run({"python3", tern.plugin.dir .. "/media-store.py"}, {
        stdin = tern.json.encode(request), timeout_ms = 15000,
    }):next(function(result, err, fresh)
        if err or not result then
            tern.log.warn("Media registration:", err or (result and result.stderr) or "backend unavailable")
            return
        end
        local ok, response = pcall(tern.json.decode, result.stdout)
        if not ok or not response.ok or result.status ~= 0 then
            tern.log.warn("Media registration:", ok and response.error or "invalid backend response")
            return
        end
        callback(response, fresh)
    end)
end

-- Native events require both a surface region and an exact string pane id.
local function operate(cx, pane, action, owner, context, onClosed)
    callStore({
        op = "window", action = action, pane_id = tostring(pane),
        owner_id = owner and tostring(owner) or nil,
        focused_id = tostring(cx.session:focused() or ""),
        window_key = tern.getenv("TERN_WINDOW_KEY") or "",
    }, function(response, fresh)
        if not response.pane_id then
            if closed[pane] then return end
            closed[pane] = true -- 0.8.3 can retain closed panes in the window snapshot.
            if onClosed and fresh then onClosed(fresh) end
            return
        end
        if context and fresh then
            local ev = {id = "main"}
            for k, v in pairs(context) do ev[k] = v end
            fresh.session:event(response.pane_id, ev)
        end
    end)
end

local function create(cx, owner, context)
    local id = cx:new_block("media.gallery", {tern.json.encode(context)}, "beside", {focus = false})
    if id then owners[id] = owner.pane
    else cx:toast("error", "Session Media could not open", "Install this plugin on the pane's host, then reload plugins") end
end

local function open(cx)
    local focused = cx.session:focused()
    if not focused then return end
    local owner, session, _host, context, panes = snapshot(cx, focused)
    if not owner then return end
    if gallery(owner) then return end
    local existing = findGallery(panes, session)
    if existing then
        owners[existing.pane] = owner.pane
        operate(cx, existing.pane, "resolve", owner.pane, context, function(fresh)
            if fresh.session:focused() == owner.pane then open(fresh) end
        end)
        if existing.tab ~= owner.tab then operate(cx, existing.pane, "move", owner.pane) end
        return
    end
    create(cx, owner, context)
end

local function activeGallery(cx, callback)
    local focused = cx.session:focused()
    if not focused then return end
    local owner, session = snapshot(cx, focused)
    if not owner then return end
    local panes = cx.session:panes()
    local found = gallery(owner) and owner or findGallery(panes, session)
    if found then callback(cx, found, owners[found.pane] or (not gallery(owner) and owner.pane or nil))
    else cx:toast("info", "Open Session Media first", "Use Media: Open session gallery") end
end

local function follow(_ev, cx)
    if not cx then return end
    local focused = cx.session:focused()
    if not focused then return end
    local owner, session, _host, context, panes = snapshot(cx, focused)
    if not owner then return end
    if gallery(owner) or owner.title == "Flow · Tern" or (owner.kind ~= "terminal" and owner.kind ~= "agent") then return end
    local found = findGallery(panes, session)
    if not found then return end
    owners[found.pane] = owner.pane
    operate(cx, found.pane, found.tab ~= owner.tab and "move" or "resolve", owner.pane, context)
end


-- Observe supported native opens without changing Tern's built-in file handling.
tern.route.open(function(req, cx)
    local ext = string.lower(string.match(req.path, "%.([^./]+)$") or "")
    if not mediaExtensions[ext] then return nil end
    local ownerPane = cx.session:focused()
    if not ownerPane then return nil end
    local pane, session, host, context = snapshot(cx, ownerPane)
    if not pane then return nil end
    if gallery(pane) then return nil end -- Retained media is already indexed.
    -- A window process must never reinterpret a remote path as a local file.
    if host.kind ~= "local" or (req.host ~= "local" and req.host ~= host.name) then return nil end
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
    return nil
end)

-- Defer registration until the window has finished loading the plugin.
tern.timer(0, function()
    tern.command({id = "open", title = "Media: Open session gallery", group = "Media", run = open})
    tern.command({id = "float", title = "Media: Float gallery", group = "Media", run = function(cx)
        activeGallery(cx, function(fresh, p, owner)
            operate(fresh, p.pane, "float", owner)
        end)
    end})
    tern.command({id = "dock", title = "Media: Dock gallery right", group = "Media", run = function(cx)
        activeGallery(cx, function(fresh, p, owner)
            operate(fresh, p.pane, "dock", owner)
        end)
    end})
    tern.command({id = "refresh", title = "Media: Refresh gallery", group = "Media", run = function(cx)
        activeGallery(cx, function(fresh, p)
            operate(fresh, p.pane, "resolve", nil, {ev = "action", act = "refresh"})
        end)
    end})
    tern.on("focus", follow)
    tern.on("title", follow)
    tern.on("pane_closed", function(ev) owners[ev.pane] = nil; closed[ev.pane] = true end)
end)
