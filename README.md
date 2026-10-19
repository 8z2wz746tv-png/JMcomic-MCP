# JMComic MCP Server

把 jmcomic（禁漫天堂 Python API）包装成 MCP 工具，让支持 MCP 的客户端
（Claude Desktop / Cherry Studio / Cursor / 自建 Agent 等）能搜索、查详情、
看排行，并把本子下载到本地。

底层用的是本仓库 `src/jmcomic`（`pip install -e .` 已装好）。
**只新增 `jmcomic_mcp/` 与几个入口文件，`src/jmcomic` 一行没动。**

---

## 1. 安装

```bash
./venv/bin/pip install -r requirements-mcp.txt   # 只装 mcp SDK
chmod +x run_mcp.sh
```

> `mcp` 依赖**没有**写进 `pyproject.toml`：上游声明 `requires-python >= 3.7`，
> 而 mcp SDK 要求 `>= 3.10`。混在一起会让上游失去 3.7 兼容性，所以单列一份
> `requirements-mcp.txt`。当前 venv 是 3.13，满足要求。

## 2. 接到宿主

宿主配置就是一个 `mcpServers` 块，各家格式一致（Claude Desktop 的
`claude_desktop_config.json`、Cherry Studio 的 MCP 设置、Cursor 的
`.cursor/mcp.json` 都是这个结构）。把路径换成你 clone 下来的实际位置：

```json
{
  "mcpServers": {
    "jmcomic": {
      "command": "/path/to/JMcomic-MCP/run_mcp.sh"
    }
  }
}
```

不用脚本也行，直接指解释器和入口：

```json
{
  "mcpServers": {
    "jmcomic": {
      "command": "/path/to/JMcomic-MCP/venv/bin/python",
      "args": ["/path/to/JMcomic-MCP/jmcomic_mcp/server.py"]
    }
  }
}
```

## 3. 工具清单

| 工具 | 副作用 | 说明 |
|---|---|---|
| `search_album` | 无 | 站内搜索，返回 album_id / 标题 / 标签 |
| `get_album_detail` | 无 | 标题、作者、标签、总页数、章节列表（含 photo_id） |
| `get_photo_detail` | 无 | 单个章节的图片数、所属本子、序号 |
| `get_ranking` | 无 | 日榜 / 周榜 / 月榜 |
| `get_favorites` | 无 | 收藏夹（**需要登录态**，见第 5 节） |
| `start_download` | **写磁盘** | 提交下载任务，立刻返回 `task_id` |
| `get_task` | 无 | 查单个任务进度：state / 百分比 / 图片数 / 错误 |
| `list_tasks` | 无 | 最近的任务列表 |
| `cancel_task` | 停止任务 | 秒级生效，见第 4 节 |

典型用法是**先查再下**：`search_album` → `get_album_detail` → `start_download`。

## 4. 两件必须知道的行为

### 写入确认门

`start_download` 是唯一会写磁盘的写操作，实现成两步：

1. 第一次调用（`confirmed=False`）**不会下载**，只返回
   `status: confirmation_required` 和一份计划（下哪些号、存到哪、多少并发）；
2. 模型把计划念给用户、拿到明确同意后，用**完全相同的参数**加 `confirmed=True`
   再调一次，才真正开始。

这是工具自身的约束，不是靠模型自觉。

### 取消的语义

`cancel_task` 是**不再开新的，收不回已经发出去的**：

- **生效是立即的**：信号一置位，之后不再发起任何新的图片请求，剩余章节直接跳过；
- **但还在传输中的那批收不回来**，它们会跑完。数量约等于当时的并发数
  （8 线程下一次大约 70 张）；
- 所以到 `state` 真正变成 `cancelled` 有一段**收尾时间**，并发越高、单张越慢就越长。
  实测 63 章 / 8 线程那次：发出取消后约 40 秒才进终态，期间拦下了 1363 张没下的图。
- 判断信号有没有吃到，看 `get_task` 的 `cancel_requested: true`，别等 `state`。

比原 GUI 那套"只在两个本子之间检查标志位"的软停止实在得多。

### 进度字段怎么读

`get_task` 返回几种口径，别只看百分比：

| 字段 | 含义 |
|---|---|
| `images` / `photos` | `已完成/总数`，最可靠 |
| `skipped_existing` | 本来就在磁盘上、被跳过的张数（见下） |
| `progress_basis` | 百分比按什么算的：`photos`（章节）或 `images`（图片） |
| `total_known` | 分母是否已经完整。**`false` 时百分比只能当参考** |

为什么要分口径：长篇多章本子的图片总数要等每章开始才知道，用"已发现的图片数"
当分母会让百分比虚高（63 章那本头一分钟就显示 87%，其实才 3/63 章）。
所以多章本子按章节算；单章本子整章页数一次性到手，反而按图片算更准。

`skipped_existing` 用来解释「为什么 0 张也算成功」：重下已经下过的本子时，
jmcomic 会跳过已存在的图、不调 after_image，所以 `images` 显示 `0/50`，
而 `skipped_existing` 是 `50` —— 不是失败，是都跳过了。

### 结果字段：记账 vs 磁盘事实

| 字段 | 是什么 |
|---|---|
| `images` | **下载器的记账**：它认为自己写成功了几个 |
| `verified` | **磁盘事实**：跑完对目录 stat 出来的文件数 / 字节数 / 空文件数 |
| `created_dirs` | 本次任务实际写入过的目录清单 |
| `error_kind` / `error_hint` | 失败原因的稳定枚举 + 一句人话 |

三者不一致时以 `verified` 为准——`verified.files` 少于 `images` 的已完成数、
或者 `empty_files > 0`，都说明有文件没真正落盘。

**清理半成品必须用 `created_dirs`**，不要靠"哪个目录的修改时间比较新"去猜：
那个猜法已经实战翻车过一次——`董卓 上+下` 比 `董卓 下` 晚 14 秒，被误判成
新产物混进了删除清单，差点把上一次下的东西删掉。

`created_dirs` 只出现在 `get_task` 的返回里；`list_tasks` 是精简快照
（只给 `created_dir_count`），免得一次返回几十条路径。

### 失败原因（error_kind）

| kind | 含义 / 下一步 |
|---|---|
| `image_cdn_dns_blackholed` | 图片 CDN 域名解析成 `0.0.0.0`，换 DNS 或走代理 |
| `image_cdn_dns_failed` | 域名解析失败，检查本机 DNS / 代理 |
| `image_cdn_unreachable` | 能解析但连不上，网络受限或站点故障 |
| `not_found` | 本子/章节不存在，号码可能有误 |
| `network_unreachable` | 所有候选域名都请求失败 |
| `dest_dir_not_writable` | 下载目录没有写权限 |
| `cancelled` | 已取消 |
| `unknown` | 未归类，看 `error` 里的原始堆栈 |

DNS 那一档是**真去查一次**再下结论的（`socket.getaddrinfo`），不是猜：
219 张全挂那次就是靠它定位到 CDN 域名被解析成 `0.0.0.0`。

## 5. 下载目录与登录态

### 下载目录

默认 `~/Downloads/JMComic`。**必须传绝对路径**（支持 `~`），相对路径会被拒绝——
MCP server 的 cwd 由宿主决定，相对路径会让文件落到不可预期的位置。

这个参数**始终覆盖**配置文件里的 `dir_rule.base_dir`。项目根目录里那堆本子就是
旧 GUI 漏了这一步造成的（当时下载目录只用来建空文件夹，从没写进 option，
jmcomic 于是用了 `os.getcwd()`）。

### 登录态（收藏夹要用）

在任意位置写一个 `option.yml`，配 `after_init` 的 login 插件；构建 option 时会
自动登录并把 cookies 带上：

```yaml
plugins:
  after_init:
    - plugin: login
      kwargs:
        username: 你的用户名
        password: 你的密码
```

然后把它的绝对路径传给工具的 `config_file` 参数（`get_favorites`、
`search_album`、`start_download` 等都支持）。客户端配置、代理、自定义域名
也都可以写在这个文件里。

## 6. 排错

- **日志全在 stderr**。stdio 模式下 stdout 是 JSON-RPC 通道，jmcomic 自带日志
  默认走 `print()`（`jm_config.default_jm_logging`），漏一行就会把宿主连接打断。
  `server.py` 在导入第三方模块前就把 `builtins.print` 和 jmcomic 的日志执行器
  都掰到了 stderr，并有专门的回归测试盯着这件事。
- 启动失败先手动跑一次看报错：`./run_mcp.sh`（它不该输出任何正常内容，
  只会有 stderr 日志）。
- 域名失效：jmcomic 会自己去拉最新域名；实在不行在 `config_file` 里写死
  `client.domain`。
- 任务历史只存在内存里，server 重启后 `get_task` 找不到旧任务。

## 7. 自测

```bash
# 离线：31 个用例（参数解析、确认门、任务生命周期、取消、进度口径、stdio 协议）
./venv/bin/python -m unittest discover -s jmcomic_mcp/tests -t .

# 联网：只读工具 + 下载路径端到端（dry run，不下载任何图片）
./venv/bin/python jmcomic_mcp/tests/live_check.py [album_id]
```

`live_check.py` 会替换掉"下载图片"这一步，只解析保存路径，用来确认
①网络/域名通 ②保存路径真的落在 `dest_dir` 下。

---

## 上游来源与许可

本仓库的 `src/jmcomic` 是 [hect0x7/JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
的副本（版本 2.6.9），**未经修改**，按 MIT 许可随附；原项目的 README 保留在
[README.upstream.md](./README.upstream.md)。

`jmcomic_mcp/`、`run_mcp.sh`、`requirements-mcp.txt` 及本 README 是在其之上的
MCP 封装层，同样遵循 MIT。上游版权归原作者所有。

本工具仅用于个人用途。它抓取的是第三方站点、内容版权归原权利人，请自行
确认所在地区的合法性；上游 README 也写了「为了减轻站点服务器压力，请不要
一次性爬取太多本子」——`start_download` 的 `image_threads` 参数就是给这个用的。
