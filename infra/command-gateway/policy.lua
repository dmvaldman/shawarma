-- Shared policy for discovery, suggestions, and execution. No request bodies are logged.
local cjson = require('cjson.safe')
local M = {}
local allowed = {stop = true, pause = true, continue = true, reset = true}

function M.trigger(command)
    if type(command) ~= 'string' then return nil end
    return command:match('^/?([^%s]+)')
end

function M.permitted(command)
    local trigger = M.trigger(command)
    return trigger ~= nil and allowed[string.lower(trigger)] == true
end

function M.execute(body)
    local data = cjson.decode(body or '')
    return type(data) == 'table' and type(data.command) == 'string'
        and data.command:sub(1, 1) == '/' and M.permitted(data.command)
end

function M.filter(body)
    local data = cjson.decode(body)
    if type(data) ~= 'table' then return '[]' end
    local result = {}
    for _, item in ipairs(data) do
        if type(item) == 'table' and M.permitted(item.trigger or item.Complete) then
            result[#result + 1] = item
        end
    end
    if #result == 0 then return '[]' end
    return cjson.encode(result)
end

function M.access()
    if ngx.req.get_method() ~= 'POST' then return end
    ngx.req.read_body()
    local body = ngx.req.get_body_data()
    if not body then
        local filename = ngx.req.get_body_file()
        if filename then
            local file = io.open(filename, 'rb')
            if file then body = file:read('*a'); file:close() end
        end
    end
    if not M.execute(body) then
        ngx.status = 400
        ngx.header.content_type = 'application/json'
        ngx.say(cjson.encode({id = 'swarm.command_not_available',
            message = 'Only /stop, /pause, /continue and /reset are available.',
            status_code = 400}))
        return ngx.exit(400)
    end
end

function M.header()
    if ngx.req.get_method() == 'GET' and ngx.status == 200 then
        ngx.ctx.command_filter = true
        ngx.header.content_length = nil
        ngx.header.etag = nil
        ngx.header.cache_control = 'no-store'
    end
end

function M.body()
    if not ngx.ctx.command_filter then return end
    ngx.ctx.chunks = ngx.ctx.chunks or {}
    ngx.ctx.chunks[#ngx.ctx.chunks + 1] = ngx.arg[1] or ''
    ngx.arg[1] = nil
    if ngx.arg[2] then
        ngx.arg[1] = M.filter(table.concat(ngx.ctx.chunks))
        ngx.ctx.chunks = nil
    end
end

return M
