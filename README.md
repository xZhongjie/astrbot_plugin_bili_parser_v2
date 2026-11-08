# astrbot_plugin_bili_parser_v2

B站视频解析（彻底重写版）。

- 解析：星之阁优先，失败回退 B 站官方 API
- 卡片：先发封面 + 信息，不等待视频
- 视频：只走第三方下载代理，多线程下载
  - 每个线程独立申请一个代理 token，避免单 token 被限流
  - 分片失败自动换 token 重试
  - 不使用 B 站 CDN 直链
- 配置只有 7 项，没有历史遗留开关

## 指令

```
/bili BV1xxxxxxx
/bili https://www.bilibili.com/video/BV1xxxxxxx
```

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `proxy_api` | kukutool | 代理接口，留空不发视频 |
| `threads` | 4 | 下载线程数 |
| `send_video` | true | 是否发视频 |
| `send_cover` | true | 是否发封面 |
| `template` | 见默认 | 卡片模板 |
| `show_error` | false | 解析失败是否提示 |

## 说明

代理接口需满足：

```
POST <proxy_api>
{"url": "<B站直链>", "sourceUrl": "<视频页>", "type": "video"}
→ {"url": "<可下载地址>"}
```

下载结果会校验总长度和 MP4 文件头，不完整不会发送。
