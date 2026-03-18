import json
import base64
import asyncio
import websockets
from flask import Flask, request, make_response, jsonify, Response
from threading import Thread
from urllib.parse import urlparse
import time
import random
import string
from collections import defaultdict, deque
import logging
import heapq
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
import threading

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except:
    app.config['JSON_AS_ASCII'] = False

# 优化的并发控制类
class ConcurrentController:
    def __init__(self, max_concurrent_per_origin=50, token_bucket_rate=100, token_bucket_capacity=200):
        self.max_concurrent_per_origin = max_concurrent_per_origin
        self.token_bucket_rate = token_bucket_rate  # 每秒补充令牌数
        self.token_bucket_capacity = token_bucket_capacity  # 最大令牌数
        
        # 每个origin的并发控制
        self.concurrent_counts = defaultdict(int)
        self.concurrent_locks = defaultdict(threading.Lock)
        
        # 令牌桶系统
        self.token_buckets = defaultdict(lambda: {
            'tokens': self.token_bucket_capacity,
            'last_refill': time.time()
        })
        
        # 优先级队列系统
        self.priority_queues = defaultdict(deque)  # FIFO队列
        self.queue_locks = defaultdict(threading.Lock)
        
        # 统计信息
        self.stats = defaultdict(lambda: {
            'total_requests': 0,
            'success_requests': 0, 
            'failed_requests': 0,
            'throttled_requests': 0,
            'avg_response_time': 0,
            'connected_at': None,
            'last_request': None
        })
        
        # 启动定时清理任务
        self.cleanup_timer = threading.Timer(30.0, self._cleanup_expired_requests)
        self.cleanup_timer.daemon = True
        self.cleanup_timer.start()
    
    def _refill_tokens(self, origin: str) -> bool:
        """填充令牌桶"""
        bucket = self.token_buckets[origin]
        now = time.time()
        elapsed = now - bucket['last_refill']
        
        # 计算应补充的令牌数
        tokens_to_add = int(elapsed * self.token_bucket_rate)
        bucket['tokens'] = min(bucket['tokens'] + tokens_to_add, self.token_bucket_capacity)
        bucket['last_refill'] = now
        
        return bucket['tokens'] > 0
    
    def acquire_token(self, origin: str, timeout: float = 10.0) -> bool:
        """获取令牌（支持超时）"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if self._refill_tokens(origin) and self.token_buckets[origin]['tokens'] > 0:
                with self.concurrent_locks[origin]:
                    if self.token_buckets[origin]['tokens'] > 0:
                        self.token_buckets[origin]['tokens'] -= 1
                        return True
            
            time.sleep(0.01)  # 短暂等待后重试
        
        return False
    
    def can_execute_request(self, origin: str) -> bool:
        """检查是否可以执行新请求"""
        with self.concurrent_locks[origin]:
            return self.concurrent_counts[origin] < self.max_concurrent_per_origin
    
    def acquire_execution_slot(self, origin: str) -> bool:
        """获取执行槽位"""
        with self.concurrent_locks[origin]:
            if self.concurrent_counts[origin] < self.max_concurrent_per_origin:
                self.concurrent_counts[origin] += 1
                return True
            return False
    
    def release_execution_slot(self, origin: str):
        """释放执行槽位"""
        with self.concurrent_locks[origin]:
            if self.concurrent_counts[origin] > 0:
                self.concurrent_counts[origin] -= 1
    
    def queue_request(self, origin: str, request_id: str, request_data: dict, priority: int = 0):
        """将请求加入队列"""
        queue_item = {
            'request_id': request_id,
            'request_data': request_data,
            'priority': priority,
            'timestamp': time.time(),
            'attempts': 0
        }
        
        with self.queue_locks[origin]:
            # 按优先级排序插入（简化实现，实际可使用heapq）
            self.priority_queues[origin].append(queue_item)
    
    def dequeue_request(self, origin: str) -> Optional[dict]:
        """从队列取出请求"""
        with self.queue_locks[origin]:
            if self.priority_queues[origin]:
                return self.priority_queues[origin].popleft()
        return None
    
    def update_stats(self, origin: str, success: bool, response_time: float = 0):
        """更新统计信息"""
        stats = self.stats[origin]
        stats['total_requests'] += 1
        if success:
            stats['success_requests'] += 1
            # 更新平均响应时间（滑动平均）
            old_avg = stats['avg_response_time']
            stats['avg_response_time'] = (old_avg * (stats['success_requests'] - 1) + response_time) / stats['success_requests']
        else:
            stats['failed_requests'] += 1
    
    def _cleanup_expired_requests(self):
        """清理过期的队列项"""
        current_time = time.time()
        for origin in list(self.priority_queues.keys()):
            with self.queue_locks[origin]:
                # 移除超过30秒的请求
                self.priority_queues[origin] = deque([
                    item for item in self.priority_queues[origin] 
                    if current_time - item['timestamp'] <= 30
                ])
        
        # 重新启动定时器
        self.cleanup_timer = threading.Timer(30.0, self._cleanup_expired_requests)
        self.cleanup_timer.daemon = True
        self.cleanup_timer.start()

# 全局控制器实例
concurrent_controller = ConcurrentController(max_concurrent_per_origin=30, token_bucket_rate=50, token_bucket_capacity=100)

# 原来的全局变量
loop = None
all_wsclient = {}
response_futures = {}

def generate_random_string(length=32):
    """生成随机字符串作为请求ID"""
    characters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def req_handle(verChar):
    """处理请求数据"""
    try:
        req_json = request.get_json()
        hmethod = req_json['method']
        hurl = base64.b64decode(req_json['url']).decode('utf-8')
        
        try:
            hheader = base64.b64decode(req_json['header']).decode('utf-8')
        except:
            hheader = ''
        
        hdata = req_json['data']
        
        data = '{}[][][][][][]{}[][][][][][]{}[][][][][][]{}'.format(
            base64.b64encode(hmethod.encode()).decode(),
            base64.b64encode(hurl.encode()).decode(),
            hdata,
            base64.b64encode(hheader.encode()).decode()
        )
        
        data = verChar + '------------' + str(data)
        data = base64.b64encode(data.encode()).decode()
        
        parsed_url = urlparse(hurl)
        result = f'{parsed_url.scheme}://{parsed_url.netloc}'
        return (data, result, verChar)
    except Exception as e:
        logger.error(f'请求处理错误: {str(e)}')
        return None

@app.route('/api', methods=['POST'])
def receive_data():
    """接收并处理API请求 - 优化并发控制版本"""
    start_time = time.time()
    verChar = generate_random_string()
    data = req_handle(verChar)
    
    if not data or not data[0]:
        logger.error('请求数据解析失败')
        return Response(b'Bad Request', status=400, content_type='text/plain')
    
    request_data, origin, request_id = data
    
    # 检查目标WebSocket客户端是否存在
    if origin not in all_wsclient:
        logger.warning(f'WebSocket客户端未连接: {origin}')
        error_msg = f'WebSocket未连接: {origin}\n请先在浏览器访问目标网站'
        return Response(error_msg.encode('utf-8'), status=503, content_type='text/plain; charset=utf-8')
    
    # 获取令牌（限流控制）
    if not concurrent_controller.acquire_token(origin, timeout=5.0):
        logger.warning(f'[{verChar[:8]}] 令牌不足，请求被限流: {origin}')
        concurrent_controller.update_stats(origin, success=False)
        return Response(b'Too Many Requests', status=429, content_type='text/plain')
    
    # 检查并发限制
    if not concurrent_controller.acquire_execution_slot(origin):
        # 超过并发限制，放入队列
        logger.info(f'[{verChar[:8]}] 并发限制，请求排队: {origin}')
        concurrent_controller.queue_request(origin, request_id, {
            'request_data': request_data,
            'start_time': start_time
        })
        # 这里可以实现排队逻辑，为了简化直接返回错误
        return Response(b'Service Unavailable - Queue Full', status=503, content_type='text/plain')
    
    # 更新统计
    concurrent_controller.update_stats(origin, success=True)
    concurrent_controller.stats[origin]['last_request'] = time.time()
    
    logger.info(f'[{verChar[:8]}] 收到请求 -> {origin} | 并发数: {concurrent_controller.concurrent_counts[origin]}')
    
    try:
        # 创建Future对象等待响应
        future = asyncio.run_coroutine_threadsafe(
            wait_for_response_with_retry(verChar, origin, request_data),
            loop
        )
        
        # 阻塞等待结果，超时90秒
        result = future.result(timeout=90)
        
        if result is None:
            concurrent_controller.update_stats(origin, success=False, response_time=time.time()-start_time)
            logger.error(f'[{verChar[:8]}] 超时无响应')
            return Response(b'Gateway Timeout', status=504, content_type='text/plain')
        
        status_str, headers_str, body_b64 = result
        
        # 检查是否是错误响应
        if status_str == '0' or headers_str == '0':
            concurrent_controller.update_stats(origin, success=False, response_time=time.time()-start_time)
            logger.error(f'[{verChar[:8]}] 浏览器请求失败')
            return Response(b'Bad Gateway', status=502, content_type='text/plain')
        
        # 计算响应时间并更新统计
        response_time = time.time() - start_time
        concurrent_controller.update_stats(origin, success=True, response_time=response_time)
        
        # 解析状态码
        try:
            status_code = int(status_str)
        except:
            status_code = 200
        
        # 解析响应头
        try:
            headers_dict = json.loads(headers_str)
        except Exception as e:
            logger.error(f'[{verChar[:8]}] 解析响应头失败: {str(e)}')
            headers_dict = {}
        
        # 解码响应体
        try:
            body_bytes = base64.b64decode(body_b64)
            logger.info(f'[{verChar[:8]}] ✓ 响应: Status={status_code}, Size={len(body_bytes)} bytes, RT={response_time:.3f}s')
        except Exception as e:
            logger.error(f'[{verChar[:8]}] 解码响应体失败: {str(e)}')
            body_bytes = b'Decode Error'
        
        # 使用 Flask Response 对象
        response = Response(
            body_bytes,
            status=status_code,
            content_type=headers_dict.get('content-type', 'application/octet-stream')
        )
        
        # 设置响应头
        skip_headers = {
            'transfer-encoding', 
            'content-encoding',
            'content-length',   
            'connection',
            'keep-alive'
        }
        
        for key, value in headers_dict.items():
            key_lower = key.lower()
            if key_lower not in skip_headers:
                try:
                    response.headers[key] = value
                except Exception as e:
                    logger.debug(f'设置响应头 {key} 失败: {str(e)}')
        
        logger.info(f'[{verChar[:8]}] ✓✓ 完整响应已返回')
        return response
        
    except asyncio.TimeoutError:
        logger.warning(f'[{verChar[:8]}] 请求超时 (90秒)')
        concurrent_controller.update_stats(origin, success=False, response_time=time.time()-start_time)
        if verChar in response_futures:
            del response_futures[verChar]
        return Response(b'Gateway Timeout', status=504, content_type='text/plain')
    except Exception as e:
        logger.error(f'[{verChar[:8]}] 处理响应异常: {str(e)}')
        import traceback
        traceback.print_exc()
        concurrent_controller.update_stats(origin, success=False, response_time=time.time()-start_time)
        return Response(b'Internal Server Error', status=500, content_type='text/plain')
    finally:
        # 释放执行槽位
        concurrent_controller.release_execution_slot(origin)

async def wait_for_response_with_retry(verChar, origin, request_data):
    """带重试机制的响应等待"""
    max_retries = 3
    retry_delay = 1.0
    
    for attempt in range(max_retries):
        try:
            event = asyncio.Event()
            response_futures[verChar] = {'event': event, 'data': None, 'created_at': time.time()}
            
            # 发送数据
            websocket = all_wsclient.get(origin)
            if websocket:
                await websocket.send(request_data)
                logger.info(f'[{verChar[:8]}] → 已发送到浏览器 (尝试 {attempt+1}/{max_retries})')
            else:
                logger.error(f'[{verChar[:8]}] WebSocket连接丢失')
                return None
            
            # 等待事件被设置，超时88秒
            await asyncio.wait_for(event.wait(), timeout=88)
            
            # 返回响应数据
            result = response_futures[verChar]['data']
            if result:
                logger.info(f'[{verChar[:8]}] ← 收到浏览器响应')
            return result
            
        except asyncio.TimeoutError:
            logger.warning(f'[{verChar[:8]}] 浏览器响应超时 (尝试 {attempt+1}/{max_retries})')
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))  # 指数退避
                continue
            return None
        finally:
            # 清理
            if verChar in response_futures:
                del response_futures[verChar]

async def handle_client(websocket, path):
    """处理WebSocket客户端连接"""
    global all_wsclient
    client_address = websocket.remote_address
    
    try:
        headers = websocket.request_headers
        origin = headers.get('Origin', '')
    except:
        origin = ''
    
    all_wsclient[origin] = websocket
    concurrent_controller.stats[origin]['connected_at'] = time.time()
    
    logger.info('=' * 70)
    logger.info(f'✓ WebSocket连接成功')
    logger.info(f'  IP: {client_address[0]}:{client_address[1]}')
    logger.info(f'  域名: {origin}')
    logger.info(f'  当前连接数: {len(all_wsclient)}')
    logger.info(f'  配置 - 最大并发: {concurrent_controller.max_concurrent_per_origin}, '
               f'令牌速率: {concurrent_controller.token_bucket_rate}/s')
    logger.info('=' * 70)
    
    message_count = 0
    last_log_time = time.time()
    last_heartbeat = time.time()
    
    try:
        async for message in websocket:
            try:
                message_count += 1
                
                # 每30秒打印一次统计
                if time.time() - last_log_time > 30:
                    stats = concurrent_controller.stats[origin]
                    logger.info(
                        f'[{origin}] 统计 - '
                        f'消息:{message_count}, '
                        f'总请求数:{stats["total_requests"]}, '
                        f'成功:{stats["success_requests"]}, '
                        f'失败:{stats["failed_requests"]}, '
                        f'平均RT:{stats["avg_response_time"]:.3f}s, '
                        f'当前并发:{concurrent_controller.concurrent_counts[origin]}, '
                        f'剩余令牌:{concurrent_controller.token_buckets[origin]["tokens"]}'
                    )
                    last_log_time = time.time()
                
                decoded_message = base64.b64decode(message).decode('utf-8', errors='ignore')
                
                # 处理心跳
                if decoded_message == '__heartbeat__':
                    last_heartbeat = time.time()
                    await websocket.send(base64.b64encode('__heartbeat_ack__'.encode()).decode())
                    continue
                
                # 处理连接就绪消息
                if decoded_message == '__connection_ready__':
                    logger.info(f'[{origin}] 收到连接就绪确认')
                    await websocket.send(base64.b64encode('__pong__'.encode()).decode())
                    continue
                
                # 处理响应消息
                sep_idx = decoded_message.find('------------')
                if sep_idx == -1:
                    continue
                
                verChar = decoded_message[:sep_idx]
                rest = decoded_message[sep_idx + 12:]
                
                # 分割响应数据
                parts = rest.split('------------')
                if len(parts) >= 3:
                    if verChar in response_futures:
                        try:
                            # 解码状态码和响应头
                            status_code = base64.b64decode(parts[0]).decode('utf-8')
                            headers_str = base64.b64decode(parts[1]).decode('utf-8')
                            
                            # body 保持 base64 编码
                            body_b64 = parts[2]
                            
                            logger.info(f'[{verChar[:8]}] ← 收到数据 Status={status_code}')
                            
                            # 设置响应数据并触发事件
                            response_futures[verChar]['data'] = (status_code, headers_str, body_b64)
                            response_futures[verChar]['event'].set()
                            
                        except Exception as e:
                            logger.error(f'[{verChar[:8]}] 解析响应错误: {str(e)}')
                            import traceback
                            traceback.print_exc()
                            # 设置错误数据
                            if verChar in response_futures:
                                response_futures[verChar]['data'] = ('0', '0', '0')
                                response_futures[verChar]['event'].set()
                    else:
                        logger.debug(f'[{verChar[:8]}] 响应ID不存在或已过期')
                
                # 检查心跳超时
                if time.time() - last_heartbeat > 120:
                    logger.warning(f'[{origin}] 心跳超时，主动关闭连接')
                    await websocket.close(1000, "心跳超时")
                    break
                    
            except Exception as e:
                logger.error(f'[{origin}] 处理消息错误: {str(e)}')
                import traceback
                traceback.print_exc()
                
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f'✗ WebSocket断开 - {origin} (代码:{e.code})')
    except Exception as e:
        logger.error(f'[{origin}] WebSocket异常: {str(e)}')
    finally:
        if origin in all_wsclient:
            all_wsclient.pop(origin)
        
        stats = concurrent_controller.stats[origin]
        logger.info('=' * 70)
        logger.info(f'✗ 连接关闭 - {origin}')
        logger.info(f'  总消息数: {message_count}')
        logger.info(f'  总请求数: {stats["total_requests"]}')
        logger.info(f'  成功: {stats["success_requests"]}, 失败: {stats["failed_requests"]}')
        if stats["connected_at"]:
            logger.info(f'  连接时长: {int(time.time() - stats["connected_at"])}秒')
        logger.info('=' * 70)

async def periodic_cleanup():
    """定期清理和统计"""
    while True:
        await asyncio.sleep(60)
        
        current_time = time.time()
        
        # 清理超时的future（超过120秒）
        to_remove = []
        for key, value in response_futures.items():
            if current_time - value['created_at'] > 120:
                to_remove.append(key)
        
        for key in to_remove:
            if key in response_futures:
                # 触发事件避免阻塞
                try:
                    response_futures[key]['event'].set()
                except:
                    pass
                del response_futures[key]
        
        if to_remove:
            logger.warning(f'清理了 {len(to_remove)} 个超时请求')

def start_ws_server():
    """启动WebSocket服务器"""
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # WebSocket服务器配置
    server = websockets.serve(
        handle_client,
        '0.0.0.0',
        8765,
        ping_interval=None,
        ping_timeout=None,
        max_size=100 * 1024 * 1024,
        max_queue=None,
        write_limit=20 * 1024 * 1024,
        read_limit=20 * 1024 * 1024,
        compression=None,
    )
    
    loop.run_until_complete(server)
    loop.create_task(periodic_cleanup())
    
    logger.info('╔' + '═' * 68 + '╗')
    logger.info('║' + ' ' * 15 + 'WebSocket服务器启动成功' + ' ' * 28 + '║')
    logger.info('║' + ' ' * 68 + '║')
    logger.info('║  监听地址: ws://0.0.0.0:8765' + ' ' * 38 + '║')
    logger.info('║  Flask API: http://127.0.0.1:8764' + ' ' * 33 + '║')
    logger.info('║  并发控制: 每域名最大{}并发, 令牌桶速率{}/s'.format(
        concurrent_controller.max_concurrent_per_origin, 
        concurrent_controller.token_bucket_rate))
    logger.info('║' + ' ' * 68 + '║')
    logger.info('║  ⚠ 重要：先在浏览器访问目标网站建立WebSocket连接' + ' ' * 18 + '║')
    logger.info('║  然后配置扫描工具使用代理并添加 qaq 请求头' + ' ' * 22 + '║')
    logger.info('╚' + '═' * 68 + '╝')
    
    loop.run_forever()

def start_flask_app():
    """启动Flask应用"""
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    
    logger.info('Flask API服务器启动 - http://127.0.0.1:8764')
    
    # 禁用 Flask 的请求日志
    import logging as flask_logging
    log = flask_logging.getLogger('werkzeug')
    log.setLevel(flask_logging.ERROR)
    
    app.run('127.0.0.1', port=8764, threaded=True, processes=1)

if __name__ == '__main__':
    flask_thread = Thread(target=start_flask_app, daemon=True)
    flask_thread.start()
    start_ws_server()