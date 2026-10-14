package.cpath = '/usr/local/openresty/lualib/?.so;' .. package.cpath
package.path = '/etc/swarm/?.lua;' .. package.path
local policy = require('policy')
local cjson = require('cjson.safe')
for _, command in ipairs({'/stop', '/pause', '/continue', '/reset', '/PAUSE', '/reset extra'}) do
    assert(policy.execute(cjson.encode({command = command})))
end
for _, command in ipairs({'/shrug', '/logout', '/msg owner hi', '/resetting', 'pause', '', ' /pause'}) do
    assert(not policy.execute(cjson.encode({command = command})))
end
assert(not policy.execute('invalid json'))
assert(not policy.execute('[]'))
local filtered = cjson.decode(policy.filter(cjson.encode({
    {trigger = 'shrug'}, {trigger = 'pause', id = 'keep-id'},
    {Complete = 'logout'}, {Complete = 'reset', Description = 'keep-description'},
    {trigger = 'resetting'},
})))
assert(#filtered == 2 and filtered[1].id == 'keep-id')
assert(filtered[2].Description == 'keep-description')
assert(policy.filter('[{"trigger":"shrug"}]') == '[]')
assert(policy.filter('invalid upstream body') == '[]')
-- Successful discovery streams may arrive in multiple chunks.
local original_ngx = ngx
_G.ngx = {ctx = {}}
ngx.ctx.command_filter = true
ngx.arg = {'[{"trigger":"pause"},', false}
policy.body()
assert(ngx.arg[1] == nil)
ngx.arg = {'{"trigger":"shrug"}]', true}
policy.body()
assert(#cjson.decode(ngx.arg[1]) == 1)
-- Non-discovery responses and error responses pass through unchanged.
ngx.ctx.command_filter = false
ngx.arg = {'unmodified', true}
policy.body()
assert(ngx.arg[1] == 'unmodified')
_G.ngx = original_ngx
print('Command allowlist checks passed.')
