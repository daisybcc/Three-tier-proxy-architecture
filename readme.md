## 系统架构概述

这是一个三层架构的代理系统：

1. **mitmproxy层** - 拦截和处理HTTP流量
2. **Flask WebSocket服务层** - 协调通信和请求管理
3. **浏览器JS层** - 在浏览器中执行实际的跨域请求

## 核心功能模块

### 1. Flask WebSocket服务器 (Python)

python

编辑



```
1# 主要功能：
2- WebSocket服务端 (端口8765)
3- HTTP API接口 (端口8764)
4- 连接管理和统计
5- 请求转发和响应处理
```

**关键技术点：**

- 异步编程 (`asyncio`)
- WebSocket通信 (`websockets`)
- Web框架 (`Flask`)
- 并发控制 (`asyncio.run_coroutine_threadsafe`)

### 2. mitmproxy插件 (Python)

python

编辑







```
1# 主要功能：
2- 拦截HTTP请求
3- 识别带qaq头的特殊请求
4- 将请求转发到本地API
5- 注入浏览器JS脚本
```

**关键技术点：**

- HTTP代理中间件
- 异步HTTP客户端 (`aiohttp`)
- 连接池优化
- 请求/响应生命周期管理

### 3. 浏览器JavaScript (前端)

javascript

编辑







```
1// 主要功能：
2- WebSocket客户端连接
3- Fetch API包装器
4- 请求/响应代理
5- 自动重连机制
```

**关键技术点：**

- WebSocket客户端
- Fetch API拦截
- ArrayBuffer二进制处理
- 内存管理 (`MAX_CONCURRENT`限制)

## 数据流转流程

1. **请求发起**：
   - 扫描工具 → mitmproxy → Flask API → WebSocket → 浏览器
2. **请求处理**：
   - 浏览器收到请求 → fetch执行 → 响应返回
3. **响应回传**：
   - 浏览器 → WebSocket → Flask → mitmproxy → 扫描工具

## 技术亮点

### 1. 编码协议设计

python

编辑







```
1# 复杂的数据打包协议
2data = '{}[][][][][][]{}[][][][][][]{}[][][][][][]{}'.format(
3    base64.b64encode(hmethod.encode()).decode(),
4    base64.b64encode(hurl.encode()).decode(), 
5    hdata,
6    base64.b64encode(hheader.encode()).decode()
7)
```

### 2. 并发控制

- 信号量限制并发请求数
- Future模式等待响应
- 超时处理机制

### 3. 连接管理

- 自动重连机制
- 心跳检测
- 连接统计和监控

### 4. 错误处理

- 多层异常捕获
- 超时恢复
- 内存泄漏防护

## 技术栈总结

- **后端**: Python + Flask + asyncio + websockets
- **代理**: mitmproxy + aiohttp
- **前端**: JavaScript + WebSocket + Fetch API
- **通信协议**: WebSocket + Base64编码
- **并发模型**: 异步I/O + 事件循环

## 创新点

1. **CORS绕过方案** - 利用浏览器同源策略信任
2. **双向代理架构** - 浏览器作为请求代理
3. **协议透明传输** - 保持原始请求/响应格式
4. **高可用设计** - 重连、心跳、超时机制

这个系统巧妙地利用浏览器的网络权限来突破CORS限制，是一种创新的代理解决方案。