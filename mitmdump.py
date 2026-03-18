from mitmproxy import http
from mitmproxy import ctx
import aiohttp
import time
import base64
import asyncio

# 极简高性能JavaScript
jstext = '''
(function() {
    let ws = null;
    let reconnectTimer = null;
    let reconnectCount = 0;
    const MAX_RECONNECT = 999999;
    const RECONNECT_DELAYS = [500, 1000, 2000, 3000, 5000];
    let hasConnectedOnce = false;
    let isManualClose = false;
    let pendingTasks = 0;
    const MAX_CONCURRENT = 50;
    
    function getReconnectDelay() {
        const index = Math.min(reconnectCount, RECONNECT_DELAYS.length - 1);
        return RECONNECT_DELAYS[index];
    }
    
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        
        try {
            ws = new WebSocket('ws://127.0.0.1:8765');
            ws.binaryType = 'arraybuffer';
            
            ws.onopen = () => {
                reconnectCount = 0;
                hasConnectedOnce = true;
                isManualClose = false;
                ws.send(btoa('__connection_ready__'));
            };
            
            ws.onclose = () => {
                if (hasConnectedOnce && !isManualClose && reconnectCount < MAX_RECONNECT) {
                    reconnectCount++;
                    reconnectTimer = setTimeout(connect, getReconnectDelay());
                } else if (!hasConnectedOnce) {
                    reconnectTimer = setTimeout(connect, 500);
                }
            };
            
            ws.onerror = () => {};
            
            ws.onmessage = (event) => {
                try {
                    const msg = atob(event.data);
                    if (msg === '__heartbeat_ack__' || msg === '__pong__') return;
                    
                    const sepIdx = msg.indexOf('------------');
                    if (sepIdx === -1) return;
                    
                    const id = msg.slice(0, sepIdx);
                    const data = msg.slice(sepIdx + 12);
                    
                    const parts = data.split('[][][][][][]');
                    if (parts.length !== 4) return;
                    
                    const method = atob(parts[0]);
                    const url = atob(parts[1]);
                    const body = decodeBase64(parts[2]);
                    const headers = atob(parts[3]);
                    
                    if (pendingTasks >= MAX_CONCURRENT) {
                        setTimeout(() => handleRequest(id, method, url, body, headers), 10);
                    } else {
                        handleRequest(id, method, url, body, headers);
                    }
                    
                } catch (e) {}
            };
            
        } catch (e) {
            if (reconnectCount < MAX_RECONNECT) {
                reconnectCount++;
                reconnectTimer = setTimeout(connect, getReconnectDelay());
            }
        }
    }
    
    function handleRequest(id, method, url, body, headers) {
        pendingTasks++;
        
        const opts = { method, mode: 'cors', credentials: 'include' };
        
        if (headers) {
            const hdrs = {};
            headers.split('|||||').forEach(line => {
                const idx = line.indexOf(':');
                if (idx > 0) {
                    hdrs[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
                }
            });
            if (Object.keys(hdrs).length > 0) {
                opts.headers = new Headers(hdrs);
            }
        }
        
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase()) && body) {
            opts.body = body;
        }
        
        fetch(url, opts)
            .then(r => {
                const status = r.status.toString();
                const hdrs = {};
                r.headers.forEach((v, k) => hdrs[k] = v);
                return r.arrayBuffer().then(buf => ({
                    status,
                    headers: JSON.stringify(hdrs),
                    body: ab2b64(buf)
                }));
            })
            .then(res => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(btoa(id + '------------' + btoa(res.status) + '------------' + btoa(res.headers) + '------------' + res.body));
                }
            })
            .catch(() => {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(btoa(id + '------------' + btoa('0') + '------------' + btoa('0') + '------------' + btoa('0')));
                }
            })
            .finally(() => {
                pendingTasks--;
            });
    }
    
    function ab2b64(buf) {
        let binary = '';
        const bytes = new Uint8Array(buf);
        const len = bytes.length;
        const chunkSize = 8192;
        
        for (let i = 0; i < len; i += chunkSize) {
            const end = Math.min(i + chunkSize, len);
            binary += String.fromCharCode.apply(null, bytes.subarray(i, end));
        }
        return btoa(binary);
    }
    
    function decodeBase64(str) {
        try {
            const pad = str.length % 4;
            if (pad) str += '='.repeat(4 - pad);
            const bin = atob(str);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) {
                bytes[i] = bin.charCodeAt(i);
            }
            return new TextDecoder().decode(bytes);
        } catch (e) {
            return '';
        }
    }
    
    setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(btoa('__heartbeat__'));
        } else if (hasConnectedOnce && (!ws || ws.readyState === WebSocket.CLOSED)) {
            connect();
        }
    }, 45000);
    
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && hasConnectedOnce && (!ws || ws.readyState !== WebSocket.OPEN)) {
            connect();
        }
    });
    
    window.addEventListener('beforeunload', () => {
        isManualClose = true;
        if (ws) ws.close();
    });
    
    connect();
})();
'''


class CustomResponse:
    def __init__(self):
        self.session = None
        self.connector = None
        self.timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=50)
        self.request_count = 0
        self.last_log_time = time.time()
        self.semaphore = None
    
    async def _get_session(self):
        """高性能连接池配置"""
        if self.session is None or self.session.closed:
            self.connector = aiohttp.TCPConnector(
                limit=10000,
                limit_per_host=1000,
                ttl_dns_cache=3600,
                force_close=False,
                enable_cleanup_closed=True,
                keepalive_timeout=300,
            )
            self.session = aiohttp.ClientSession(
                connector=self.connector,
                timeout=self.timeout,
                auto_decompress=True,
                trust_env=False,
                connector_owner=True,
                skip_auto_headers=['User-Agent'],
            )
            
            if self.semaphore is None:
                self.semaphore = asyncio.Semaphore(1000)
            
            ctx.log.info(f"[Session] 初始化 - 总连接:10000, 单host:1000")
        return self.session
    
    async def request(self, flow: http.HTTPFlow) -> None:
        """
        处理请求
        关键修复：在这里处理带 qaq 头的请求，直接设置 flow.response
        这样就不会进入 response() 函数，避免被浏览器拦截
        """
        headers_lower = {k.lower(): v for k, v in flow.request.headers.items()}
        flag_req = headers_lower.get('qaq', '')
        
        if flag_req:
            # 扫描工具的请求 - 转发模式
            ctx.log.info(f"[Scanner Request] {flow.request.method} {flow.request.url}")
            await self._get_session()
            async with self.semaphore:
                await self.handle_delayed_request(flow)
            # 注意：这里已经设置了 flow.response，不会再进入 response() 函数

    async def handle_delayed_request(self, flow: http.HTTPFlow) -> None:
        """处理扫描工具的请求 - 核心修复点"""
        self.request_count += 1
        
        if time.time() - self.last_log_time > 10:
            ctx.log.info(f"[Stats] {self.request_count} req/10s")
            self.last_log_time = time.time()
            self.request_count = 0
        
        # 构建请求头
        newheaders = '|||||'.join([
            f'qwqwqwqwqw: {value}' if key.lower() == 'qaq' else f'{key}: {value}' 
            for key, value in flow.request.headers.items()
        ])
        
        # 构建请求数据
        try:
            request_body = flow.request.text if flow.request.text else ''
        except:
            request_body = flow.request.content.decode('utf-8', errors='ignore') if flow.request.content else ''
        
        burp0_json = {
            'data': base64.b64encode(request_body.encode('utf-8')).decode(),
            'header': base64.b64encode(newheaders.encode('utf-8')).decode(),
            'method': str(flow.request.method),
            'url': base64.b64encode(flow.request.url.encode('utf-8')).decode()
        }
        
        try:
            session = await self._get_session()
            
            # 发送到 server
            async with session.post(
                'http://127.0.0.1:8764/api',
                headers={'Content-Type': 'application/json;charset=UTF-8'},
                json=burp0_json,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=90)
            ) as response:
                
                # 读取完整响应内容
                content = await response.read()
                
                # 构建响应头
                resp_headers = http.Headers()
                for key, value in response.headers.items():
                    if key.lower() not in ['server', 'transfer-encoding']:
                        resp_headers[key] = value
                
                # 关键修复：直接构建并设置 flow.response
                # 这样响应就直接返回给扫描工具，不会进入 response() 函数
                flow.response = http.Response.make(
                    status_code=response.status,
                    content=content,
                    headers=resp_headers
                )
                
                ctx.log.info(f"[Scanner Response] ✓ Status={response.status}, Size={len(content)} bytes -> 直接返回给扫描工具")
                
        except asyncio.TimeoutError:
            ctx.log.error(f"[Timeout] {flow.request.url}")
            flow.response = http.Response.make(504, b"Gateway Timeout")
            
        except aiohttp.ClientError as e:
            ctx.log.error(f"[ClientError] {str(e)}")
            flow.response = http.Response.make(502, b"Bad Gateway")
            
        except Exception as e:
            ctx.log.error(f"[Error] {str(e)}")
            flow.response = http.Response.make(500, b"Internal Server Error")

    def response(self, flow: http.HTTPFlow) -> None:
        """
        处理响应 - 只处理浏览器的普通请求
        关键修复：扫描工具的请求不会到达这里，因为在 request() 中已经设置了 flow.response
        """
        headers_lower = {k.lower(): v for k, v in flow.request.headers.items()}
        resflag = headers_lower.get('qwqwqwqwqw', '')
        flag_req = headers_lower.get('qaq', '')
        
        # 只在浏览器的普通请求时注入 JS
        # 带 qaq 或 qwqwqwqwqw 头的请求不会到达这里
        if not flag_req and not resflag:
            content_type = flow.response.headers.get('Content-Type', '').lower()
            
            if content_type.startswith('application/javascript'):
                try:
                    flow.response.text += jstext
                except:
                    pass
            elif content_type.startswith('text/html') and flow.response.text:
                try:
                    if not flow.response.text.startswith("{"):
                        flow.response.text += '<script>' + jstext + '</script>'
                except:
                    pass
            
            if 'Content-Security-Policy' in flow.response.headers:
                del flow.response.headers['Content-Security-Policy']
    
    def done(self):
        """清理资源"""
        if self.session and not self.session.closed:
            ctx.log.info("[Session] Closing...")
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self.session.close())
            loop.close()


addons = [CustomResponse()]