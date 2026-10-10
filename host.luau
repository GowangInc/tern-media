-- Native media.gallery; binary image data never enters the saved UI state.
local ui = tern.ui
local PAGE = 12
local scopes = {"conversation", "workspace", "all"}
local kinds = {"all", "image", "video", "audio", "document"}

-- Decode/resize away from the host callback. Every image comes from retained bytes,
-- never the mutable source path. Small output keeps blob callbacks bounded.
local imageReader = [=[
import base64,json,os,shutil,stat,subprocess,sys,tempfile
try:
    path,size,mime=sys.argv[1],int(sys.argv[2]),sys.argv[3]
    info=os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size>64*1024*1024:
        raise ValueError('Retained image is not a regular supported-size file')
    with tempfile.TemporaryDirectory(prefix='tern-media-image-') as d:
        output=os.path.join(d,'preview.png')
        sips=shutil.which('sips')
        ffmpeg=shutil.which('ffmpeg')
        data=b''
        for bound in (size,max(120,size//2),max(120,size//4)):
            converted=False
            if sips:
                result=subprocess.run([sips,'-s','format','png','-Z',str(bound),path,'--out',output],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
                converted=result.returncode==0
            if not converted and ffmpeg:
                result=subprocess.run([ffmpeg,'-y','-v','error','-i',path,'-vf',"scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease"%(bound,bound),'-frames:v','1',output],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
                converted=result.returncode==0
            if converted:
                mime='image/png'
                source=output
            elif not sips and not ffmpeg:
                source=path
            else:
                raise ValueError('Image conversion unavailable; open the retained original')
            with open(source,'rb') as f: data=f.read(1024*1024+1)
            if len(data)<=1024*1024: break
        if len(data)>1024*1024:
            raise ValueError('Image preview exceeds 1 MiB; open the retained original')
        print(json.dumps({'ok':True,'data':base64.b64encode(data).decode('ascii'),'mime':mime}))
except Exception as e:
    print(json.dumps({'ok':False,'error':str(e)}))
    sys.exit(1)
]=]

local function member(values, value, fallback)
    return table.find(values, value) and value or fallback
end

local function context(value)
    local out = {}
    if type(value) == "table" then
        for _, field in {"pane_id", "window_key", "tern_session_id", "tern_session_name", "host", "cwd", "label"} do
            if value[field] ~= nil then out[field] = tostring(value[field]) end
        end
    end
    return out
end

local function update(cx)
    cx:render()
    cx:save()
end

local function request(payload, callback)
    -- Spawn and serialization are deferred, including requests initiated by init.
    tern.timer(0, function()
        local ok, err = pcall(function()
            tern.process.run({"python3", tern.plugin.dir .. "/media-store.py"}, {
                stdin = tern.json.encode(payload), timeout_ms = 30000,
            }, function(result)
                local decoded, response = pcall(tern.json.decode, result.stdout)
                if not decoded or type(response) ~= "table" then
                    callback(nil, result.timed_out and "Media request timed out" or "Media backend returned no valid JSON")
                elseif result.status ~= 0 or not response.ok then
                    callback(nil, response.error or "Media backend request failed")
                else
                    callback(response, nil)
                end
            end)
        end)
        if not ok then callback(nil, tostring(err)) end
    end)
end

local function ownerQuery(s)
    local q = {op = "list", scope = "conversation", limit = 1}
    if s.owner.pane_id and s.owner.pane_id ~= "" then q.pane_id = s.owner.pane_id end
    if s.owner.window_key and s.owner.window_key ~= "" then q.window_key = s.owner.window_key end
    return q
end

local function sessionFor(s, resolved)
    local session = resolved and table.clone(resolved) or table.clone(s.owner)
    -- Do not change the identity of an OMP conversation or erase its useful label.
    for _, field in {"pane_id", "window_key", "tern_session_id", "tern_session_name", "host", "cwd"} do
        if s.owner[field] and s.owner[field] ~= "" then session[field] = s.owner[field] end
    end
    if not session.label or session.label == "" then session.label = s.owner.label or "Terminal media" end
    if not session.id then
        session.id = "terminal:" .. (s.owner.host or "local") .. ":" .. (s.owner.window_key or "") .. ":" .. (s.owner.pane_id or "")
    end
    return session
end

local function errorsText(errors)
    local lines = {}
    for i, err in errors or {} do
        if i > 4 then table.insert(lines, "Additional errors: " .. tostring(#errors - 4)); break end
        table.insert(lines, tostring(err.name or "Media") .. ": " .. tostring(err.error or err))
    end
    return table.concat(lines, "\n")
end

local function chosen(s)
    for _, item in s.items do if item.id == s.selected then return item end end
    return nil
end

local refresh
local pumpImages

local function queueImage(s, item, size, cx)
    local path = size == 240 and item.preview_path or (item.kind == "image" and item.stored_path or item.preview_path)
    if not path then return nil end
    local key = item.sha256 .. ":" .. tostring(size)
    if not s.images[key] then
        s.images[key] = {loading = true}
        table.insert(s.imageQueue, {key = key, path = path, size = size, mime = path == item.preview_path and "image/png" or item.mime})
        tern.timer(0, function() pumpImages(s, cx) end)
    end
    return s.images[key]
end

pumpImages = function(s, cx)
    if s.imageJobs >= 2 or #s.imageQueue == 0 then return end
    local job = table.remove(s.imageQueue, 1)
    s.imageJobs += 1
    local function complete(response, err)
        local entry = s.images[job.key]
        entry.loading = false
        if response then
            local ok, blob = pcall(function() return cx:blob(tern.base64.decode(response.data), response.mime) end)
            if ok then entry.blob = blob else entry.error = "Could not load image: " .. tostring(blob) end
        else
            entry.error = err
        end
        s.imageJobs -= 1
        cx:render()
        tern.timer(0, function() pumpImages(s, cx) end)
    end
    local ok, err = pcall(function()
        tern.process.run({"python3", "-c", imageReader, job.path, tostring(job.size), job.mime}, {timeout_ms = 20000}, function(result)
            local decoded, response = pcall(tern.json.decode, result.stdout)
            if decoded and type(response) == "table" and response.ok and result.status == 0 then
                complete(response, nil)
            else
                complete(nil, decoded and type(response) == "table" and response.error or "Image preview unavailable; open retained original")
            end
        end)
    end)
    if not ok then complete(nil, tostring(err)) end
    tern.timer(0, function() pumpImages(s, cx) end)
end

local function prepareImages(s, cx)
    for _, item in s.items do queueImage(s, item, 240, cx) end
    local item = chosen(s)
    if item then queueImage(s, item, 1200, cx) end
    for _, slot in {"A", "B"} do
        if s.compare[slot] then queueImage(s, s.compare[slot], 1200, cx) end
    end
end

refresh = function(s, cx)
    s.generation += 1
    local generation = s.generation
    s.loading = true
    s.error = nil
    update(cx)
    local function failure(err)
        if generation ~= s.generation then return end
        s.loading = false
        s.error = err
        update(cx)
    end
    local function listing()
        if generation ~= s.generation then return end
        local q = {op = "list", scope = s.scope, kind = s.kind, search = s.search, offset = s.offset, limit = PAGE}
        if s.pin then
            q.session_id = s.pin.id
            if s.scope == "workspace" then q.tern_session_id = s.pin.tern_session_id end
        else
            q.pane_id = s.owner.pane_id
            if s.owner.window_key and s.owner.window_key ~= "" then q.window_key = s.owner.window_key end
        end
        request(q, function(response, err)
            if generation ~= s.generation then return end
            if not response then failure(err); return end
            if response.total > 0 and s.offset >= response.total then
                s.offset = math.floor((response.total - 1) / PAGE) * PAGE
                listing()
                return
            end
            s.items, s.total = response.items, response.total
            s.session, s.sessions, s.watches = response.session, response.sessions, response.watch_roots
            s.loading = false
            s.error = errorsText(response.errors)
            if s.error == "" then s.error = nil end
            s.contentsLabel = s.scope == "all" and "All archived media" or (response.session and response.session.label or "No conversation binding")
            if not chosen(s) then s.selected = s.items[1] and s.items[1].id or nil end
            prepareImages(s, cx)
            update(cx)
        end)
    end
    -- Resolve the current pane before any touch; viewing never fabricates a terminal
    -- conversation and a missing workspace never widens to all media.
    if s.pin or not s.owner.pane_id or s.owner.pane_id == "" then listing(); return end
    request(ownerQuery(s), function(response, err)
        if generation ~= s.generation then return end
        if not response then failure(err); return end
        local resolved = response.session
        s.bound = resolved
        local needsTouch = false
        if resolved then
            for _, field in {"tern_session_id", "tern_session_name", "cwd", "host"} do
                if s.owner[field] and s.owner[field] ~= "" and resolved[field] ~= s.owner[field] then needsTouch = true end
            end
        end
        if needsTouch then
            request({op = "touch", session = sessionFor(s, resolved)}, function(touched, touchError)
                if generation ~= s.generation then return end
                if not touched then failure(touchError); return end
                s.bound = touched.session
                listing()
            end)
        else listing() end
    end)
end

local function beginEdit(s, mode, value)
    s.edit = {mode = mode, text = value or "", caret = #(value or "") + 1}
end

local function mutate(s, cx, payload, success)
    if s.busy then return end
    s.busy, s.error = true, nil
    update(cx)
    request(payload, function(response, err)
        s.busy = false
        if not response then s.error = err; update(cx); return end
        if success then success(response) end
        refresh(s, cx)
        local errors = errorsText(response.errors)
        if errors ~= "" then s.operationErrors = errors; cx:render() end
    end)
end

local function attach(s, cx, mode, path)
    if s.busy then return end
    if path == "" then s.error = "Enter an explicit host-local path"; return end
    if not s.pin and (not s.owner.pane_id or s.owner.pane_id == "") then
        s.error = "No owner pane. Open the gallery from the terminal you want to associate."; return
    end
    s.busy, s.error = true, nil
    update(cx)
    local function finish(session)
        local payload
        if mode == "import" then
            payload = {op = "register", session = session, items = tern.json.array({{path = path, source = "transferred"}})}
        else payload = {op = "watch", session_id = session.id, path = path, enabled = true} end
        request(payload, function(response, err)
            s.busy = false
            if not response then s.error = err; update(cx); return end
            s.edit = nil
            s.notice = mode == "watch" and "Directory associated; only direct, non-hidden files are watched." or "Retained import completed."
            s.operationErrors = errorsText(response.errors)
            refresh(s, cx)
        end)
    end
    if s.pin then finish(table.clone(s.pin)); return end
    -- Resolve again at submission, since OMP can bind a new UUID while the path is typed.
    local owner = table.clone(s.owner)
    local q = ownerQuery(s)
    request(q, function(response, err)
        if not response then s.busy = false; s.error = err; update(cx); return end
        local snapshot = {owner = owner}
        local session = sessionFor(snapshot, response.session)
        request({op = "touch", session = session}, function(touched, touchError)
            if not touched then s.busy = false; s.error = touchError; update(cx); return end
            finish(touched.session)
        end)
    end)
end

local function submit(s, cx)
    local edit = s.edit
    if not edit or s.busy then return end
    if edit.mode == "search" then
        s.search, s.offset, s.edit = edit.text, 0, nil
        refresh(s, cx)
    elseif edit.mode == "tags" then
        local tags = tern.json.array()
        for tag in string.gmatch(edit.text, "[^,]+") do
            local clean = string.match(tag, "^%s*(.-)%s*$")
            if clean ~= "" and not table.find(tags, clean) then table.insert(tags, clean) end
        end
        mutate(s, cx, {op = "tag", id = edit.item_id, tags = tags}, function(response)
            s.edit = nil
            for _, slot in {"A", "B"} do
                if s.compare[slot] and s.compare[slot].id == response.item.id then s.compare[slot] = response.item end
            end
            s.notice = "Tags saved."
        end)
    else attach(s, cx, edit.mode, edit.text) end
end

local function setSlot(s, slot, cx)
    local item = chosen(s)
    if not item or item.kind ~= "image" then s.error = "Select an image to assign comparison " .. slot; return end
    s.compare[slot] = table.clone(item)
    s.comparing = true
    queueImage(s, item, 1200, cx)
end

local function selectItem(s, id, cx)
    for _, item in s.items do
        if item.id == id then s.selected = id; queueImage(s, item, 1200, cx); return end
    end
end

local function action(s, act, value, cx)
    if act == "refresh" then
        for key, image in s.images do if image.error then s.images[key] = nil end end
        s.operationErrors, s.notice = nil, nil
        refresh(s, cx)
    elseif act == "scope" and table.find(scopes, value) then s.scope, s.offset = value, 0; refresh(s, cx)
    elseif act == "kind" and table.find(kinds, value) then s.kind, s.offset = value, 0; refresh(s, cx)
    elseif act == "previous" and s.offset > 0 then s.offset = math.max(0, s.offset - PAGE); refresh(s, cx)
    elseif act == "next" and s.offset + PAGE < s.total then s.offset += PAGE; refresh(s, cx)
    elseif act == "select" then selectItem(s, tonumber(value), cx)
    elseif act == "search" then beginEdit(s, "search", s.search)
    elseif act == "clear-search" then s.search, s.offset = "", 0; refresh(s, cx)
    elseif act == "tags" then
        local item = chosen(s)
        if item then beginEdit(s, "tags", table.concat(item.tags, ", ")); s.edit.item_id = item.id end
    elseif act == "import" or act == "watch" then beginEdit(s, act)
    elseif act == "submit" then submit(s, cx)
    elseif act == "cancel" then s.edit = nil
    elseif act == "compare-a" then setSlot(s, "A", cx)
    elseif act == "compare-b" then setSlot(s, "B", cx)
    elseif act == "compare" then s.comparing = not s.comparing
    elseif act == "clear-compare" then s.compare, s.comparing = {}, false
    elseif act == "open" then
        local item = chosen(s)
        if item then cx:open(item.stored_path) end
    elseif act == "open-a" or act == "open-b" then
        local item = s.compare[act == "open-a" and "A" or "B"]
        if item then cx:open(item.stored_path) end
    elseif act == "archives" then s.archives = not s.archives
    elseif act == "archive-prev" then s.archiveOffset = math.max(0, s.archiveOffset - 8)
    elseif act == "archive-next" and s.archiveOffset + 8 < #s.sessions then s.archiveOffset += 8
    elseif act == "pin" then
        local target = nil
        if not value then target = s.session end
        if value then for _, session in s.sessions do if session.id == value then target = session; break end end end
        if target then s.pin, s.scope, s.offset, s.archives = table.clone(target), "conversation", 0, false; refresh(s, cx) end
    elseif act == "follow" then s.pin, s.offset = nil, 0; refresh(s, cx)
    elseif act == "unwatch" then
        local session = s.pin or s.session
        if session then mutate(s, cx, {op = "watch", session_id = session.id, path = value, enabled = false}) end
    end
end

local function button(text, act, disabled, active)
    return ui.el("button", {
        text = text, actions = {click = act}, disabled = disabled or false,
        class = "media-control" .. (active and " media-active" or ""),
        attrs = {["aria-label"] = text},
    })
end

local function column(class, children)
    return ui.el("div", {class = class}, children)
end

local function text(value, style)
    return ui.text(ui.span(value, style))
end

local function imageView(s, item, size)
    local entry = s.images[item.sha256 .. ":" .. tostring(size)]
    if entry and entry.blob then
        -- The installed BlockCx API documents image.blob; sizing is CSS, not
        -- invented TSP image fields. The name/version caption supplies alt context.
        return ui.node("image", {blob = entry.blob})
    elseif entry and entry.error then return text(entry.error, "error")
    elseif entry and entry.loading then return text("Loading retained image…", "muted") end
    return text(string.upper(item.kind) .. " · no thumbnail · open retained original", "muted")
end

local function preview(s, item, label, openAction)
    if not item then return column("media-slot", {text(label .. " · Select an image, then assign " .. label, "muted")}) end
    return column("media-slot", {
        text(label .. " · " .. item.name .. " · v" .. tostring(item.version), "strong"),
        imageView(s, item, 1200),
        text(item.recorded_at .. " · " .. item.session_label, "muted"),
        button("Open retained " .. label, openAction),
    })
end

local function editorView(s)
    local e = s.edit
    local labels = {search = "Search names and tags", tags = "Tags · comma-separated labels", import = "Import file · verbatim host-local path", watch = "Watch directory · nonrecursive, no hidden files or symlinks"}
    local draft = ui.text({
        ui.span(string.sub(e.text, 1, e.caret - 1)),
        ui.span("│", "strong"),
        ui.span(string.sub(e.text, e.caret)),
    })
    return column("media-editor", {
        text(labels[e.mode], "strong"), draft,
        text("Type or paste · ←/→ caret · Home/End · Backspace/Delete · Enter apply · Esc cancel", "muted"),
        ui.row({button(s.busy and "Saving…" or "Apply", "submit", s.busy), button("Cancel", "cancel", s.busy)}),
    })
end

tern.block.define("gallery", {
    init = function(cx, args, saved)
        local parsed = {}
        if args[1] then
            local ok, value = pcall(tern.json.decode, args[1])
            if ok then parsed = context(value) end
        end
        local keep = type(saved) == "table" and saved or {}
        local s = {
            owner = parsed, scope = member(scopes, keep.scope, "conversation"), kind = member(kinds, keep.kind, "all"),
            search = type(keep.search) == "string" and keep.search or "",
            offset = math.max(0, tonumber(keep.offset) or 0), selected = keep.selected,
            pin = type(keep.pin) == "table" and keep.pin.id and keep.pin or nil,
            compare = type(keep.compare) == "table" and keep.compare or {}, comparing = keep.comparing == true,
            items = {}, sessions = {}, watches = {}, total = 0, generation = 0, loading = true,
            images = {}, imageQueue = {}, imageJobs = 0, archiveOffset = 0, archives = false,
        }
        tern.timer(0, function() prepareImages(s, cx); refresh(s, cx) end)
        return s
    end,
    title = function(s) return s.pin and "Media · " .. s.pin.label or "Session Media" end,
    save = function(s)
        return {scope = s.scope, kind = s.kind, search = s.search, offset = s.offset,
            selected = s.selected, pin = s.pin, compare = s.compare, comparing = s.comparing}
    end,
    event = function(s, ev, cx)
        if ev.ev ~= "action" then return end
        if ev.act == "context" then
            local owner = context(ev)
            local changed = owner.pane_id ~= s.owner.pane_id or owner.window_key ~= s.owner.window_key or owner.tern_session_id ~= s.owner.tern_session_id
            s.owner = owner
            if not s.pin then
                if changed then
                    s.offset, s.selected, s.items, s.total, s.session, s.bound = 0, nil, {}, 0, nil, nil
                    s.compare, s.comparing, s.edit = {}, false, nil
                end
                refresh(s, cx)
            end
        else action(s, ev.act, ev.value, cx) end
    end,
    key = function(s, key, cx)
        local name = key.name
        if s.edit then
            local e = s.edit
            if name == "escape" or name == "esc" then if not s.busy then s.edit = nil end; return true end
            if s.busy then return false end
            if name == "enter" then submit(s, cx); return true end
            if name == "home" then e.caret = 1; return true end
            if name == "end" then e.caret = #e.text + 1; return true end
            if name == "left" then e.caret = utf8.offset(e.text, -1, e.caret) or 1; return true end
            if name == "right" then e.caret = utf8.offset(e.text, 2, e.caret) or (#e.text + 1); return true end
            if name == "backspace" and e.caret > 1 then
                local previous = utf8.offset(e.text, -1, e.caret) or 1
                e.text = string.sub(e.text, 1, previous - 1) .. string.sub(e.text, e.caret)
                e.caret = previous
                return true
            end
            if name == "delete" and e.caret <= #e.text then
                local after = utf8.offset(e.text, 2, e.caret) or (#e.text + 1)
                e.text = string.sub(e.text, 1, e.caret - 1) .. string.sub(e.text, after)
                return true
            end
            if key.text and (name == "paste" or (not key.ctrl and not key.meta and not key.alt)) then
                local inserted = string.gsub(key.text, "[\r\n]", " ")
                if #e.text + #inserted <= 8192 then
                    e.text = string.sub(e.text, 1, e.caret - 1) .. inserted .. string.sub(e.text, e.caret)
                    e.caret += #inserted
                else s.error = "Field exceeds 8192 bytes" end
                return true
            end
            return false
        end
        if key.ctrl or key.meta or key.alt then return false end
        if name == "up" or name == "left" or name == "down" or name == "right" then
            local index = 1
            for i, item in s.items do if item.id == s.selected then index = i; break end end
            local delta = (name == "up" or name == "left") and -1 or 1
            local item = s.items[math.clamp(index + delta, 1, math.max(1, #s.items))]
            if item then selectItem(s, item.id, cx) end
            return item ~= nil
        end
        local shortcuts = {r = "refresh", a = "compare-a", b = "compare-b", c = "compare", t = "tags", i = "import", w = "watch", o = "open", ["/"] = "search", ["["] = "previous", ["]"] = "next", p = s.pin and "follow" or "pin", h = "archives"}
        if name == "escape" or name == "esc" then s.comparing, s.archives = false, false; return true end
        if name == "s" then action(s, "scope", scopes[(table.find(scopes, s.scope) or 1) % #scopes + 1], cx); return true end
        if name == "k" then action(s, "kind", kinds[(table.find(kinds, s.kind) or 1) % #kinds + 1], cx); return true end
        local act = shortcuts[name]
        if act then action(s, act, nil, cx); return true end
        return false
    end,
    view = function(s, cx)
        local narrow = cx.cols < 60
        local controls = {
            text(s.pin and ("Pinned · " .. s.pin.label) or "Following owner conversation", "strong"),
            ui.row({button("Current", "scope=conversation", false, s.scope == "conversation"), button("Workspace", "scope=workspace", false, s.scope == "workspace"), button("All archives", "scope=all", false, s.scope == "all")}),
            ui.row({button(s.pin and "Follow current [P]" or "Pin session [P]", s.pin and "follow" or "pin", not s.pin and not s.session), button("Archived sessions [H]", "archives"), button("Refresh [R]", "refresh", s.busy)}),
        }
        local filters = {}
        for _, kind in kinds do table.insert(filters, button(kind, "kind=" .. kind, false, s.kind == kind)) end
        table.insert(controls, narrow and column("media-kinds", filters) or ui.row(filters))
        table.insert(controls, ui.row({button(s.search == "" and "Search [/]" or ("Search: " .. s.search), "search"), button("Clear search", "clear-search", s.search == "")}))
        table.insert(controls, ui.row({button("Import file [I]", "import", s.busy), button("Watch directory [W]", "watch", s.busy)}))
        local parts = {column("media-toolbar", controls)}
        if s.archives then
            local entries = {text("Pin a conversation by identity · labels may repeat", "strong")}
            for i = s.archiveOffset + 1, math.min(#s.sessions, s.archiveOffset + 8) do
                local session = s.sessions[i]
                table.insert(entries, button(session.label .. " · " .. session.id, "pin=" .. session.id))
            end
            if #s.sessions == 0 then table.insert(entries, text("No archived conversations on this host", "muted")) end
            table.insert(entries, ui.row({button("Earlier sessions", "archive-prev", s.archiveOffset == 0), button("More sessions", "archive-next", s.archiveOffset + 8 >= #s.sessions)}))
            table.insert(parts, column("media-archives", entries))
        end
        if s.loading then table.insert(parts, ui.progress(nil, "Loading session media…")) end
        if s.busy then table.insert(parts, text("Saving media association…", "muted")) end
        if s.error then table.insert(parts, column("media-status media-error", {text("Error · " .. s.error, "error"), text("Last successful contents: " .. (s.contentsLabel or "none") .. ". Refresh to retry.", "muted")})) end
        if s.operationErrors and s.operationErrors ~= "" then table.insert(parts, text("Import report · " .. s.operationErrors, "error")) end
        if s.notice then table.insert(parts, text(s.notice, "muted")) end
        if s.edit then table.insert(parts, editorView(s)) end
        if s.comparing then
            local slots = {preview(s, s.compare.A, "A", "open-a"), preview(s, s.compare.B, "B", "open-b")}
            table.insert(parts, column("media-compare", {narrow and ui.col(slots) or ui.row(slots), ui.row({button("Exit compare [Esc]", "compare"), button("Clear A/B", "clear-compare")})}))
        else
            local item = chosen(s)
            if item then table.insert(parts, column("media-preview", {text(item.name .. " · retained version " .. tostring(item.version), "strong"), imageView(s, item, 1200)})) end
        end
        local item = chosen(s)
        if item then
            table.insert(parts, column("media-detail", {
                text(item.recorded_at .. " · " .. item.source .. " · " .. item.session_label, "muted"),
                text("Tags: " .. (#item.tags > 0 and table.concat(item.tags, ", ") or "none")),
                ui.row({button("Open retained [O]", "open"), button("Edit tags [T]", "tags", s.busy)}),
                ui.row({button("Assign A [A]", "compare-a", item.kind ~= "image"), button("Assign B [B]", "compare-b", item.kind ~= "image"), button(s.comparing and "Show preview" or "Compare A/B [C]", "compare")}),
            }))
        end
        local ledger = {text((s.contentsLabel or "Session timeline") .. " · " .. tostring(s.total) .. " retained items", "strong")}
        if #s.items == 0 and not s.loading then
            table.insert(ledger, text((s.search ~= "" or s.kind ~= "all") and "No matching media. Clear search or select all kinds." or "No retained media in this scope. Import an explicit file or associate a watch directory. Missing conversation/workspace bindings remain empty.", "muted"))
        end
        for _, entry in s.items do
            table.insert(ledger, column("media-card" .. (entry.id == s.selected and " media-selected" or ""), {
                button(entry.name .. " · v" .. tostring(entry.version), "select=" .. tostring(entry.id), false, entry.id == s.selected),
                imageView(s, entry, 240),
                text(entry.recorded_at .. " · " .. entry.kind .. " · " .. entry.source, "muted"),
            }))
        end
        table.insert(parts, column("media-ledger", ledger))
        table.insert(parts, ui.row({button("Previous [", "previous", s.loading or s.offset == 0), text(tostring(s.total == 0 and 0 or s.offset + 1) .. "–" .. tostring(math.min(s.offset + PAGE, s.total)) .. " / " .. tostring(s.total)), button("Next ]", "next", s.loading or s.offset + PAGE >= s.total)}))
        if #s.watches > 0 then
            local watches = {text("Associated directories · direct files only", "strong")}
            for _, path in s.watches do table.insert(watches, button("Stop watching: " .. path, "unwatch=" .. path, s.busy)) end
            table.insert(parts, column("media-watches", watches))
        end
        return {
            main = ui.col({column("media-gallery", parts)}),
            dock = ui.col({text("Arrows select · A/B assign · C compare · R refresh · / search · T tags · I import · W watch · O open · P pin/follow · H archives · S scope · K kind · [ / ] page · Esc close", "muted")}),
        }
    end,
})
