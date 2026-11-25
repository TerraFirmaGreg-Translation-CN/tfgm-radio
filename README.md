# TFG 广播电台：Navidrome → 24/7 随机音乐流

给群峦格雷（TerraFirmaGreg-Modern）里的 **Etched 收音机** 提供一个“永远在播、谁连上就听当前这首”的 URL。

```
Navidrome (Subsonic API)
        │  getRandomSongs → 随机曲目
        │  stream?id=…   → 原始音频（flac/ogg/mp3/…）
        ▼
   radio.py  ── ffmpeg 解码 → 单路 MP3 160k 编码 ──► 共享环形缓冲
                                                      │
                              ┌───────────────────────┼───────────────────────┐
                         听众 A（HTTP 分块流）     听众 B                 听众 C
```

**一个编码器、一份字节流**：所有听众读的是同一条流，新听众从“当前播放位置”接入（`--prebuffer-kb` 之后），所以是广播，不是各自随机。

## 为什么必须是“流”，不能做成唱片

Etched 的收音机（`SoundTracker.playRadio`）用 `AudioFileType.BOTH` 打开 URL：响应没有 `Content-Length` 时走流式分支，直接读 HTTP 连接，不落盘、不缓存，`setLoop(true)` 只对 EOF 生效——无限流永远不 EOF。

而蚀刻台唱片走 `AudioFileType.FILE`：会先整段下载进缓存，单文件上限 100 MB，遇到 chunked 无限流直接报 `The provided URL is a stream, but that is not supported`。**所以电台只能进收音机，不能刻成唱片。**

## 依赖

- `python3` ≥ 3.8（只用标准库）
- `ffmpeg`，需带 `libmp3lame`：`ffmpeg -encoders | grep libmp3lame`（用镜像时已内置）
- Navidrome，Subsonic API 默认开启

## 启动

```bash
export NAVIDROME_PASSWORD='你的密码'
python3 radio.py \
  --navidrome-url http://127.0.0.1:4533 \
  --user radio \
  --listen 0.0.0.0:8080 \
  --bitrate 160 \
  --token '换成随机串'
```

输出：

```
2026-11-25 18:52:45 INFO queue refilled with 50 track(s)
2026-11-25 18:52:45 INFO playing 万能青年旅店 - 杀死那个石家庄人
2026-11-25 18:52:45 INFO streaming on http://0.0.0.0:8080/radio.mp3?k=…
```

浏览器/命令行自检：

```bash
curl -N "http://127.0.0.1:8080/radio.mp3?k=…" -o /tmp/x.mp3   # 录一段
ffprobe /tmp/x.mp3                                            # mp3 / 44100 / stereo
curl "http://127.0.0.1:8080/nowplaying"                       # 当前曲目、听众人数
```

`--dir /path/to/music` 可脱离 Navidrome 用本地文件调试。

## Docker（镜像由 GitHub Actions 构建）

`ghcr.io/terrafirmagreg-translation-cn/tfgm-radio:latest`，`linux/amd64` + `linux/arm64`。每次 push 到 `main` 或打 tag 都会先跑冒烟测试（起容器 → 读一段合成音频 → 校验 `audio/mpeg`、`chunked`、无 `Content-Length` → 用镜像里的 `ffprobe` 解码），通过后才推镜像。

```bash
docker run -d --name tfg-radio --restart unless-stopped -p 8080:8080 \
  -e NAVIDROME_URL=http://navidrome:4533 \
  -e NAVIDROME_USER=radio \
  -e NAVIDROME_PASSWORD='密码' \
  -e RADIO_TOKEN='随机串' \
  ghcr.io/terrafirmagreg-translation-cn/tfgm-radio:latest
```

compose 版本见 `deploy/docker-compose.yml`：

```bash
cd deploy && cp .env.example .env    # 填 NAVIDROME_PASSWORD、RADIO_TOKEN
docker compose up -d
```

每个命令行参数都有同名环境变量，容器里不用写参数：

| 环境变量 | 参数 | 默认 |
|---|---|---|
| `NAVIDROME_URL` | `--navidrome-url` | 必填 |
| `NAVIDROME_USER` / `NAVIDROME_PASSWORD` | `--user` / `--password` | 必填 |
| `NAVIDROME_MUSIC_FOLDER` | `--music-folder` | 全部媒体库 |
| `RADIO_GENRE` | `--genre` | 不限 |
| `RADIO_DIR` | `--dir` | —（本地文件调试） |
| `RADIO_LISTEN` | `--listen` | `0.0.0.0:8080` |
| `RADIO_TOKEN` | `--token` | 无 |
| `RADIO_BITRATE` | `--bitrate` | `160` |
| `RADIO_SAMPLE_RATE` / `RADIO_CHANNELS` | 同名参数 | `44100` / `2` |
| `RADIO_PREBUFFER_KB` | `--prebuffer-kb` | `64` |
| `RADIO_BUFFER_MB` | `--buffer-mb` | `16` |
| `FFMPEG` | `--ffmpeg` | `ffmpeg` |

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bitrate` | 160 | kbit/s；每个听众的带宽 ≈ 这个值 |
| `--prebuffer-kb` | 64 | 新听众回追多少音频。64 KB ≈ 3 s：延迟换抗抖动；降到 16 就是 0.8 s |
| `--buffer-mb` | 16 | 环形缓冲；≈13 分钟音频。太小会让慢客户端被跳过 |
| `--token` | 无 | 要求所有请求带 `?k=…`；对公网暴露时务必设置 |
| `--genre` / `--music-folder` | 无 | 只随机某风格 / 某媒体库 |
| `--ffmpeg` | `ffmpeg` | 二进制路径（或环境变量 `FFMPEG`） |

## 常驻

仓库里 `deploy/tfg-radio.service` 是可直接改用的单元模板（下为等价内容）。

`/etc/tfg-radio.env`（`chmod 600`）：

```ini
NAVIDROME_PASSWORD=你的密码
```

`/etc/systemd/system/tfg-radio.service`：

```ini
[Unit]
Description=TFG Navidrome radio
After=network-online.target navidrome.service
Wants=network-online.target

[Service]
User=radio
EnvironmentFile=/etc/tfg-radio.env
ExecStart=/usr/bin/python3 /opt/tfg-radio/radio.py \
    --navidrome-url http://127.0.0.1:4533 --user radio \
    --listen 0.0.0.0:8080 --token 换成随机串
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now tfg-radio
journalctl -u tfg-radio -f
```

## 反向代理（要 HTTPS 时）

```nginx
location /radio.mp3 {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_buffering off;          # 必须，否则 nginx 攒包，延迟和内存都炸
    proxy_cache off;
    gzip off;
    proxy_read_timeout 1h;
}
```

不加缓冲是关键：`proxy_buffering on`（默认）会把响应攒在 nginx 里，收音机变成“每 4 KB 顿一下”。

## 游戏内接线

1. 合成 **收音机**：红合金单线 ×2、铜弹簧、锻铁板 ×2、音符盒、木板 ×3（KubeJS 配方）。
2. 放下后**必须给红石信号**（拉杆/红石火把）——`POWERED` 为假时 `playRadio` 直接返回，静音无提示。
3. 右键打开 GUI，URL 填 `http://<服务器>:8080/radio.mp3?k=<token>`，回车保存。
4. 声音以方块为中心衰减（8 格距离），大范围听需要多点布置或直接每人自己放一台。
5. 每台收音机各自建立连接——**同一时刻大家听到的是同一首歌**，个体差异只有“谁先连、缓冲多少”。

## 限制（都已实测确认）

- **不是采样级同步**：实测两名听众相隔 5 s 连接，字节偏移正好 4.93 s；同时连的两人拿到完全相同的字节（md5 相同）。
- **慢客户端会被跳过**：落后超过环形缓冲时 `skips` 计数 +1，直接跳到当前，不会拖慢别人。
- **不能快进/暂停/选曲**，也不能刻成唱片（见上）。
- **不要开 ICY 元数据**（`icy-metaint`）：Etched 的 MP3 解码器不做 ICY 去交织；本服务只发 `icy-name/icy-genre/icy-br` 头，不插元数据。
- **曲目边界有 ~26 ms 编码器延迟静音**：每首歌独立编码，`-id3v2_version 0 -write_xing 0` 保证字节流里没有 ID3/Xing 垃圾帧，跨曲解码无报错（实测 39 s 连续解码 1501 帧，参数恒定）。
- **带宽** = 听众数 × bitrate：10 人 = 1.6 Mbit/s 上行。
- 服务端始终在编码（无人听也播），CPU 约 1 核的一小部分。
