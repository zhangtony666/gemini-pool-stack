# AGENTS.md — 给 AI 助手的安装与使用指南

> **本文档的目标读者是 AI 编码助手。** 如果你是人类用户,请直接读 [README.md](README.md)。
>
> 本文件包含完整、自洽的上下文。**不需要读取其它文件即可完成安装、配置和排障。**

---

## 0. 你是来做什么的

用户克隆了这个仓库,希望你帮他把服务跑起来。典型请求:

- "帮我装一下这个项目"
- "启动服务,然后看看状态"
- "帮我导入一个账号"
- "连不上,帮我查查原因"

**你的任务边界:**

| 做 | 不做 |
|---|---|
| 检查环境、装依赖、启动服务 | 不要修改 `app/` 下的应用源码 |
| 读懂用户的环境并适配配置 | 不要替用户注册或购买任何账号 |
| 诊断错误并给出具体修复 | 不要用 Chrome 导 cookie(原因见 §6) |
| 向用户解释每一步在做什么 | 不要跳过 `doctor` 自检 |

**所有操作都在用户的本地机器上。** 服务默认只监听 `127.0.0.1`,不对外暴露。

---

## 1. 这是什么

一个把多个 Google 账号的会话聚合成统一接口的本地服务栈。

**要解决的问题:** 单个 Google 账号的图片查询额度有限(约 10 次)。把 N 个账号集中到池中自动轮换,可用量扩到 N 倍,而使用侧无感。

**最终使用形态:** 用户在 Cherry Studio(或任何 OpenAI 兼容客户端)里拖入图片、提问,拿到判定结果。账号切换和出口轮换在后台自动完成。

---

## 2. 架构

三个进程,靠标准协议串联:

```
① Cherry Studio / 任意 OpenAI 客户端        ← 用户界面
        │  OpenAI Chat Completions
        ├──────────────────────────────┐
        │                              │
        ▼                              ▼
② image-verifier  :8083           ③ gemini-web2api-go  :8084
   业务后端(Python/FastAPI)          资源网关(Go 单二进制)
   批次 / 队列 / 重试 / 落库          Cookie 池 / 代理池 / 轮换 / 保活
        │                              │
        └──────────┬───────────────────┘
                   │  OpenAI Chat Completions
                   ▼
        ④ gemini.google.com            ← 上游,真正的判定者
```

**关键设计:职责分离**

| | ② 业务后端 | ③ 资源网关 |
|---|---|---|
| 管什么 | 批次、队列、重试、结果落库 | 账号、IP、轮换、保活、TLS 指纹 |
| 知道账号吗 | **不知道** | 全部知道 |
| 知道批次吗 | 全部知道 | **不知道** |
| 数据库 | `app/data/*.sqlite3` | `<exe 同级>/data/gemini.db` |

**加账号、换 IP 只动 ③,不需要碰 ② 的代码。**

**② 是可选的。** 如果用户只需要"在窗口里拖张图问一句",可以让客户端直连 ③,跳过 ②。

---

## 3. 环境要求

| 组件 | 要求 | 必需? | 说明 |
|---|---|---|---|
| PowerShell | **7.0+** | 必需 | Windows 5.1 不兼容,脚本会拒绝运行 |
| uv | 任意版本 | 跑 ② 才需要 | 安装脚本会自动装 |
| Firefox | 任意版本 | 导入账号才需要 | **Chrome 不行**,原因见 §6 |
| Cherry Studio | 任意版本 | 用 GUI 才需要 | 也可以用别的 OpenAI 兼容客户端 |
| Go / Docker / Node | — | **不需要** | 网关是编译好的单文件 |

网关程序(`gemini-web2api-go.exe`,约 17 MB)**不需要预装**,`install.ps1` 会自动下载。

---

## 4. 标准安装流程

按顺序执行。每一步都可以重复运行,已完成的部分会自动跳过。

### 4.1 自检

```powershell
pwsh -File scripts/stack.ps1 doctor
```

**先跑这个。** 它会报告:工具链是否齐全、网关程序在不在、Python 环境是否就绪、端口是否被占、代理能否连通 Google。

根据输出决定下一步。如果显示"一切就绪"可以直接跳到 4.3。

### 4.2 安装

```powershell
pwsh -File scripts/install.ps1
```

它做五件事:

1. 检查 PowerShell 7,不合格就报错退出
2. 没装 uv 就自动装
3. 从模板生成 `config/stack.psd1`
4. 下载网关程序并**校验 SHA256**(不匹配会中止并删除文件)
5. `uv sync --frozen` 安装 Python 依赖

**幂等,可以反复跑。**

如果只想补某一步:

```powershell
pwsh -File scripts/install.ps1 -SkipGateway    # 已有网关程序
pwsh -File scripts/install.ps1 -SkipPython     # 只下网关
pwsh -File scripts/fetch-gateway.ps1 -Force    # 只重下网关
```

### 4.3 配置代理

**这一步最容易出错,必须确认。**

`gemini.google.com` 在多数网络环境下无法直连。用户需要一个 HTTP 代理。

编辑 `config/stack.psd1`:

```powershell
ProxySeed = 'http://127.0.0.1:7890'
```

常见本地代理端口:

| 软件 | 端口 |
|---|---|
| Clash / Mihomo / Clash Verge | 7890 |
| V2RayN | 10809 |
| sing-box | 2080 |

**先问用户他的代理端口是多少**,或者替他检测:

```powershell
# 看哪些端口在监听
Get-NetTCPConnection -State Listen |
    Where-Object { $_.LocalAddress -eq '127.0.0.1' } |
    Select-Object LocalPort, OwningProcess | Sort-Object LocalPort
```

确认代理可用:

```powershell
# 应该返回 200
curl.exe -x 'http://127.0.0.1:7890' -sS -o NUL -w '%{http_code}' https://gemini.google.com/
```

**如果需要用户名密码**,格式是 `http://user:pass@host:port`。

### 4.4 启动

```powershell
pwsh -File scripts/stack.ps1 start -Only gateway
```

先只起网关,确认它能跑通。成功标志是输出里出现 `网关 已启动` 且 Cookie 池数量可见。

然后:

```powershell
pwsh -File scripts/stack.ps1 status
```

预期输出形如:

```
运行状态
────────
  网关              运行中 v4.20.1 :8084

  Cookie 池        0 启用 / 共 0      ← 还没有账号,正常
  代理池             1 个
  业务后端            未运行 :8083
```

### 4.5 导入第一个账号

```powershell
pwsh -File scripts/account.ps1 import-firefox -Label 'acc01'
```

**这个命令要求用户已经在 Firefox 里登录了 Gemini。** 如果没有,提示用户先去做。

先用 `-DryRun` 验证解析:

```powershell
pwsh -File scripts/account.ps1 import-firefox -DryRun
```

它应该输出找到的 7 个 cookie 字段和总长度。

### 4.6 验证

```powershell
pwsh -File scripts/account.ps1 list
```

期望看到该账号的 `健康=ok`。

然后实测一次图片调用(见 §7)。

### 4.7 接入客户端

```powershell
pwsh -File scripts/connect-cherry.ps1
```

这会唤起 Cherry Studio 的服务商导入页。**剩余步骤需要用户在 GUI 里操作**,告诉用户:

1. 确认并保存服务商
2. 进入「模型」页 → 点「同步模型」
3. 添加 `gemini-3.6-flash`
4. **编辑该模型 → 输入模态 → 视觉 → 打开**(不开的话传图会被客户端拦住)

---

## 5. 命令速查

所有命令在**仓库根目录**执行。

### 服务控制

```powershell
pwsh -File scripts/stack.ps1 doctor          # 环境自检(排障第一步)
pwsh -File scripts/stack.ps1 config          # 显示当前生效配置
pwsh -File scripts/stack.ps1 start           # 启动全部
pwsh -File scripts/stack.ps1 start -Only gateway    # 只起网关
pwsh -File scripts/stack.ps1 start -Only verifier   # 只起后端
pwsh -File scripts/stack.ps1 start -MockVerifier    # 后端用 mock(不消耗额度)
pwsh -File scripts/stack.ps1 start -Foreground      # 网关前台运行,看实时日志
pwsh -File scripts/stack.ps1 stop            # 停止全部
pwsh -File scripts/stack.ps1 restart         # 重启
pwsh -File scripts/stack.ps1 status          # 状态 + 资源池
pwsh -File scripts/stack.ps1 logs            # 网关最近日志
```

### 账号管理

```powershell
pwsh -File scripts/account.ps1 list                    # 列出池中账号及健康状态
pwsh -File scripts/account.ps1 profiles                # 列出 Firefox 配置文件
pwsh -File scripts/account.ps1 import-firefox          # 从 Firefox 导入(推荐)
pwsh -File scripts/account.ps1 import-firefox -DryRun  # 只解析不写入
pwsh -File scripts/account.ps1 import-firefox -Profile 'default-release' -Label 'acc02'
pwsh -File scripts/account.ps1 import-file -Path 'D:\c.json' -Label 'acc03'
pwsh -File scripts/account.ps1 import-string -Cookie 'SID=...; HSID=...; ...'
pwsh -File scripts/account.ps1 check-all               # 全池有效性检测
pwsh -File scripts/account.ps1 disable -Id 3           # 停用
pwsh -File scripts/account.ps1 remove  -Id 3           # 删除
```

### 其它

```powershell
pwsh -File scripts/install.ps1                # 安装 / 修复
pwsh -File scripts/fetch-gateway.ps1          # 只下载网关
pwsh -File scripts/connect-cherry.ps1         # 注册到 Cherry Studio
```

---

## 6. 关键约束(违反了就会失败)

### ⚠️ 必须用 Firefox 导 cookie

**这是最重要的一条。**

Chrome 新版启用了 **Device Bound Session Credentials**,导出的会话绑定在特定设备上。`__Secure-1PSIDTS` 令牌无法换发,会返回 401,会话大约 **半小时到几小时**就失效。

Firefox 没有这层设备绑定,同一套保活机制可以长期续期。

**如果用户用的是 Chrome,必须让他改用 Firefox 重新登录。**

### ⚠️ cookie 必须包含 SAPISID

网关需要 `SAPISID` 来计算 `SAPISIDHASH` 授权头。缺了它,所有请求都会被拒绝。

必需的 7 个字段:

```
SID  HSID  SSID  APISID  SAPISID  __Secure-1PSID  __Secure-1PSIDTS
```

`import-firefox` 会自动提取这 7 个。手工粘贴时最容易漏的是 `SAPISID`。

### ⚠️ 不能用 `document.cookie` 读取

这些 cookie 中除 `SSID` 外全部标记为 **HttpOnly**,在浏览器 Console 里执行 `document.cookie` 读不到任何内容。必须通过:

- 存储面板(F12 → 存储 → Cookie)
- 浏览器扩展(Cookie-Editor)
- **直接读 `cookies.sqlite` 文件**(`import-firefox` 走的就是这条路)

### ⚠️ 两个端口不能相同

`GatewayPort` 和 `VerifierPort` 必须不同,否则第二个服务起不来。脚本会检测并提示。

### ⚠️ 失效账号会挡住整个服务

**这是最容易踩的坑,务必向用户说明。**

默认配置下,**只要 Cookie 池里有账号,网关就一律使用它** —— 即使该账号已经失效,也不会自动降级到匿名模式。池子为空时才会走匿名。

表现:池里有一个失效账号,所有请求都返回 `502`,错误信息形如:

```
cookie 池里 N 个账号都不可用(最后一个:no SNlM0e in page (cookie expired or not signed in))
```

**处理方式(二选一):**

```powershell
# 方案 A:清掉失效账号(推荐)
pwsh -File scripts/account.ps1 list          # 找出失效的
pwsh -File scripts/account.ps1 remove -Id 3  # 删掉

# 方案 B:让服务在账号不可用时降级匿名
#   管理面板「设置」页 → 打开 fallback_anon
```

> **`no SNlM0e in page` 的含义**:网关需要从 Gemini 页面 HTML 里抓取 `SNlM0e` 令牌来构造请求。抓不到说明拿到的页面是登录页 —— 也就是这个 cookie 已经没有登录态了。

**因此:定期巡检 Cookie 池是必须的运维动作。**

### ⚠️ 账号额度不会被自动追踪

**网关不认识"额度"这个概念。** 它的账号轮换是纯 LRU(最久未用优先),**不知道**某个账号的配额是否已经耗尽。

后果:额度用完的账号**不会自动跳过**,而是继续被派活,然后请求失败。

项目也**不会自动摘除**失效账号。必须人工巡检:

```powershell
pwsh -File scripts/account.ps1 list
```

关注 `失败` 计数和 `最近成功` 时间。失败次数上来的账号应该手动 `disable`。

**告诉用户这一点。** 如果他打算上 100 个账号,不要期待能完全放手。

---

## 7. 验证一次真实调用

导入账号后,用它验证整条链路。**需要先拿到 API key:**

```powershell
pwsh -File scripts/stack.ps1 config
```

配置里 `GatewayApiKey` 那一项会在首次读取后自动填充。或者直接读:

```powershell
. ./scripts/lib/common.ps1
$cfg = Get-StackConfig
$exe = Get-GatewayExe -Config $cfg
Get-GatewayApiKey -Config $cfg -GatewayExe $exe
```

### Python 版本

```python
import base64, httpx
from PIL import Image
import io

key = "<你的 API key>"
buf = io.BytesIO()
Image.new("RGB", (64, 64), "red").save(buf, format="PNG")
data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

payload = {
    "model": "gemini-3.6-flash",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What color is this image? One word."},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}],
}
r = httpx.post("http://127.0.0.1:8084/v1/chat/completions",
               headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=180)
print(r.status_code, r.text[:400])
```

**成功:** `200` + 内容里包含颜色词。
**失败 400** + `image input needs a Google account cookie` → cookie 池是空的或已失效。

### PowerShell 版本

```powershell
$key = '<你的 API key>'
$body = '{"model":"gemini-3.6-flash","messages":[{"role":"user","content":"Say OK"}]}'
Invoke-RestMethod 'http://127.0.0.1:8084/v1/chat/completions' `
    -Method Post -Headers @{ Authorization = "Bearer $key" } `
    -ContentType 'application/json' -Body $body -TimeoutSec 120
```

这一条不需要 cookie,可以单独用来验证网关和代理是否正常。

---

## 8. 错误对照表

| 现象 | 原因 | 解决 |
|---|---|---|
| `找不到网关程序` | 没下载 | `pwsh -File scripts/fetch-gateway.ps1` |
| `找不到可用的 Python` | 没装 uv | `pwsh -File scripts/install.ps1` |
| 启动时 `bind: Only one usage of each socket address` | 端口被占用 | `stack.ps1 stop`,或改 `config/stack.psd1` 里的端口 |
| 网关 502,`dial tcp ... connectex` | 无法直连 Google | 配置代理(`ProxySeed`),或代理没运行 |
| `image input needs a Google account cookie` | cookie 池空 | `account.ps1 import-firefox` |
| 上传图片返回 400 | 没打开视觉输入 | 在 Cherry Studio 里编辑模型 → 输入模态 → 视觉 = 开 |
| cookie 检测显示"无效" | 会话过期 / 用了 Chrome | 改用 Firefox 重新登录再导 |
| 导入提示"缺少字段" | cookie 不完整 | 检查是否含 `SAPISID`;用 `-DryRun` 看解析结果 |
| 半小时后 cookie 就失效 | 用了 Chrome 导出 | **改用 Firefox** |
| `401 Unauthorized`(网关 API) | API key 不对 | `stack.ps1 config` 看当前值,或从面板「设置」页读取 |
| `uv sync` 失败 | 网络或锁文件问题 | 检查网络;确认 `app/uv.lock` 存在 |

### 排障顺序

遇到问题时**按这个顺序查**,不要跳步:

```powershell
# 1. 环境层面
pwsh -File scripts/stack.ps1 doctor

# 2. 服务是否在跑
pwsh -File scripts/stack.ps1 status

# 3. 网关日志
pwsh -File scripts/stack.ps1 logs

# 4. 代理能否到 Google
curl.exe -x 'http://127.0.0.1:7890' -sS -o NUL -w '%{http_code}' https://gemini.google.com/

# 5. 账号是否有效
pwsh -File scripts/account.ps1 check-all
```

---

## 9. 目录结构

```
<仓库根>/
├── app/                          Python 业务后端(不要改)
│   ├── src/image_verifier/       应用源码
│   ├── tests/                    测试
│   ├── pyproject.toml
│   ├── uv.lock
│   └── .env                      后端配置(按需创建)
│
├── config/
│   ├── stack.example.psd1        配置模板(有注释,不要改)
│   └── stack.psd1                实际配置(首次运行自动生成)
│
├── scripts/
│   ├── lib/common.ps1            公共模块:配置/路径/密钥/进程
│   ├── install.ps1               一键安装
│   ├── stack.ps1                 服务控制 ★ 主入口
│   ├── account.ps1               账号管理
│   ├── fetch-gateway.ps1         下载网关
│   └── connect-cherry.ps1        注册到 Cherry Studio
│
├── gemini-web2api-go.exe         网关程序(install 时自动下载)
├── data/gemini.db                网关数据:Cookie 池 + 代理池
└── SHA256SUMS.txt                网关校验和
```

**网关的数据目录跟着 exe 走。** 如果 `GatewayExe` 指向别处,数据库也在那个目录旁边。

---

## 10. 配置文件详解

`config/stack.psd1`

```powershell
@{
    # 网关程序路径。留空 = 自动探测(仓库根 / bin\ / 同级 gemini-gateway\)
    GatewayExe = ''

    # 端口,必须不同
    GatewayPort  = 8084
    VerifierPort = 8083

    # 留空 = 首次启动自动生成 / 读取,并写回本文件
    AdminToken    = ''
    GatewayApiKey = ''

    # 出口代理。留空 = 不预置,改用管理面板添加
    ProxySeed = 'http://127.0.0.1:7890'

    GatewayHidden    = $true    # $true = 后台运行
    OpenAdminOnStart = $false   # 启动后自动开面板
}
```

### 关于两个自动生成的密钥

- **AdminToken** — 首次 `start` 时随机生成(`adm-` + 48 位十六进制),写回配置。之后固定。
- **GatewayApiKey** — 首次读取时从网关数据库提取(`sk-gemini-...`),写回配置。

**回写后配置文件的注释会丢失**(PowerShell 数据文件的固有限制)。这是正常的。想重新生成,把对应字段清空再启动。

---

## 11. 迁移到另一台机器

**网关不需要任何运行时。** 但 PowerShell 7 和 uv 要在新机器上装。

1. 拷贝整个仓库目录到新机器
2. 装 PowerShell 7:`winget install Microsoft.PowerShell`
3. 跑 `pwsh -File scripts/install.ps1`
4. 检查 `config/stack.psd1` 里的 `ProxySeed`(代理端口可能不同)
5. 如果换了机器,建议清空 `AdminToken` 和 `GatewayApiKey` 让系统重新生成

**脚本内没有任何硬编码的绝对路径**,路径都是相对定位的。

---

## 12. 给 AI 的操作原则

### 执行前

- **先跑 `doctor`**,不要盲目开始
- 涉及删除 / 停止的操作,先告诉用户你要做什么
- 修改配置文件前,先读一遍当前内容

### 执行中

- 命令失败时,**读完整错误信息**再下结论,不要猜
- 需要用户操作的地方(登录 Firefox、GUI 配置),**明确停下来告诉他做什么**,不要试图自动化浏览器
- 不要跳过 SHA256 校验

### 执行后

- 用 `status` 或一次真实调用验证结果,不要只报告"命令成功了"
- 明确区分**已验证**和**未验证**的部分

### 不要做的事

- ❌ 不要修改 `app/` 下的应用源码。这是上游项目。
- ❌ 不要让用户用 Chrome 导 cookie。
- ❌ 不要承诺账号额度能被精确利用。网关不追踪额度。
- ❌ 不要试图自动登录 Google 账号。这必须由用户手动完成。
- ❌ 不要把 `config/stack.psd1` 提交到版本库(里面有密钥)。

---

## 13. 上游项目

| 项目 | 作用 | 许可 |
|---|---|---|
| [zexadev/gemini-web2api-go](https://github.com/zexadev/gemini-web2api-go) | 资源网关 | MIT |
| [CherryHQ/cherry-studio](https://github.com/CherryHQ/cherry-studio) | 桌面客户端 | Apache-2.0 |

网关是**独立下载**的,不随本仓库分发。`fetch-gateway.ps1` 从官方 Releases 拉取并校验完整性。
