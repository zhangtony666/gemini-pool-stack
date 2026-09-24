# image-verifier-stack

把多个 Google 账号聚合成一个统一接口,在桌面客户端里直接使用。

单个 Google 账号的图片查询额度有限(约 10 次)。这个栈把 N 个账号集中到池中自动轮换,可用量扩到 N 倍 —— **而你在界面上拖张图、问一句,感觉不到背后在换账号。**

---

## 它能做什么

- **号池化** — 多个 Google 账号自动轮换,按"最久未用优先"
- **出口绑定** — 每个账号自动粘住一个出口 IP,长期不变
- **自动保活** — 会话令牌定期刷新,不用手动维护
- **统一接口** — 对上层暴露标准 OpenAI 协议,任何兼容客户端都能接
- **任务持久化**(可选)— 批量提交、结果落库、进程重启不丢任务

---

## 架构

```
① Cherry Studio / 任意 OpenAI 客户端       ← 你的操作界面
        │  OpenAI Chat Completions
        ▼
② image-verifier  :8083                   ← 业务后端(可选)
   批次 · 队列 · 重试 · 结果落库
        │  OpenAI Chat Completions
        ▼
③ gemini-web2api-go  :8084                ← 资源网关
   Cookie 池 · 代理池 · 轮换 · 保活 · TLS 指纹
        │  Web 协议
        ▼
④ gemini.google.com                       ← 上游服务
```

**职责是分开的:** 加账号、换 IP 只动 ③,不用碰 ② 的代码。

② 是**可选的**。只想要"在窗口里拖图问一句",可以让客户端直连 ③。

---

## 快速开始

**环境要求:** Windows + PowerShell 7。[装 PowerShell 7 →](https://aka.ms/powershell)

```powershell
# 1. 安装(检查环境、下载网关、装依赖)
pwsh -File scripts/install.ps1

# 2. 自检
pwsh -File scripts/stack.ps1 doctor

# 3. 启动网关
pwsh -File scripts/stack.ps1 start -Only gateway

# 4. 导入一个账号(需先在 Firefox 登录 Gemini)
pwsh -File scripts/account.ps1 import-firefox -Label 'acc01'

# 5. 接入 Cherry Studio
pwsh -File scripts/connect-cherry.ps1
```

**网关程序会自动下载,不需要装 Go、Docker 或 Node。**

---

## 前置准备

### 一个能访问 Google 的代理

`gemini.google.com` 在多数网络环境下无法直连。编辑 `config/stack.psd1`:

```powershell
ProxySeed = 'http://127.0.0.1:7890'
```

常见本地代理端口:Clash / Mihomo `7890`,V2RayN `10809`,sing-box `2080`。

验证代理可用:

```powershell
curl.exe -x 'http://127.0.0.1:7890' -sS -o NUL -w '%{http_code}' https://gemini.google.com/
# 期望输出 200
```

### Firefox(不是 Chrome)

> **必须用 Firefox 导出 cookie。**
>
> Chrome 新版启用了 Device Bound Session Credentials,导出的会话绑定在特定设备上,`__Secure-1PSIDTS` 无法续期 —— **大约半小时就会失效**。
>
> Firefox 没有这层绑定,会话可以长期保持。

在 Firefox 里登录 `gemini.google.com`,然后:

```powershell
pwsh -File scripts/account.ps1 import-firefox -Label 'acc01'
```

脚本会直接读取 Firefox 的 `cookies.sqlite`,**不需要任何手工复制**。

---

## 命令一览

所有命令在仓库根目录执行。

### 服务

```powershell
pwsh -File scripts/stack.ps1 doctor       # 环境自检(出问题先跑这个)
pwsh -File scripts/stack.ps1 start        # 启动全部
pwsh -File scripts/stack.ps1 stop         # 停止
pwsh -File scripts/stack.ps1 restart      # 重启
pwsh -File scripts/stack.ps1 status       # 状态 + 资源池
pwsh -File scripts/stack.ps1 logs         # 网关日志
```

只启一个:

```powershell
pwsh -File scripts/stack.ps1 start -Only gateway
pwsh -File scripts/stack.ps1 start -Only verifier
pwsh -File scripts/stack.ps1 start -MockVerifier    # 后端用 mock,不消耗额度
```

### 账号

```powershell
pwsh -File scripts/account.ps1 list                    # 看池子里有什么
pwsh -File scripts/account.ps1 import-firefox          # 从 Firefox 导入
pwsh -File scripts/account.ps1 import-firefox -DryRun  # 只解析,不写入
pwsh -File scripts/account.ps1 check-all               # 全池有效性检测
pwsh -File scripts/account.ps1 disable -Id 3           # 停用某个账号
pwsh -File scripts/account.ps1 remove  -Id 3           # 删除
```

### 其它

```powershell
pwsh -File scripts/install.ps1             # 安装 / 修复
pwsh -File scripts/fetch-gateway.ps1       # 只下载网关
pwsh -File scripts/connect-cherry.ps1      # 注册到 Cherry Studio
```

---

## 接入 Cherry Studio

```powershell
pwsh -File scripts/connect-cherry.ps1
```

会唤起 Cherry Studio 的导入页。之后在界面里:

1. 确认并保存服务商
2. 进入「模型」页 → 点「同步模型」
3. 添加 `gemini-3.6-flash`
4. **编辑该模型 → 输入模态 → 视觉 → 打开**

第 4 步不能省 —— 不开的话,传图片会被客户端直接拦下,请求根本到不了服务端。

---

## 管理面板

网关自带一个中文管理面板:

```
http://127.0.0.1:8084/admin
```

登录 token 在 `config/stack.psd1` 的 `AdminToken` 字段(首次启动时自动生成)。

面板里可以:

- 查看请求记录、延迟、实际使用的模型
- 管理 Cookie 池(导入 / 检测 / 启停)
- 管理代理池(增删改 / 熔断状态)
- 实时调整限流参数

---

## 配置

`config/stack.psd1` 是唯一需要修改的文件:

```powershell
@{
    GatewayExe = ''                            # 留空自动探测

    GatewayPort  = 8084                        # 两个端口必须不同
    VerifierPort = 8083

    AdminToken    = ''                         # 留空自动生成
    GatewayApiKey = ''                         # 留空自动读取

    ProxySeed = 'http://127.0.0.1:7890'        # 出口代理

    GatewayHidden    = $true                   # 后台运行
    OpenAdminOnStart = $false
}
```

前两个密钥字段留空即可,首次启动时脚本会自动生成 / 读取并写回本文件。

---

## 使用建议

### 别跑满额度

网关不认识"额度"这个概念。它的账号轮换是**纯 LRU**,不知道某个账号的配额是否已耗尽。

后果:额度用完的账号**不会自动跳过**,而是继续被派活、然后请求失败。

**建议单账号只用额度的 60–70%,留出余量。**

### 定期巡检

项目**不会自动摘除**失效账号。养成这个习惯:

```powershell
pwsh -File scripts/account.ps1 list
```

关注每个账号的 `失败` 计数和 `最近成功` 时间。失败次数上来的,手动停掉:

```powershell
pwsh -File scripts/account.ps1 disable -Id 3
```

### 先小批量验证

别一次性导入几十个账号。**先用 5 个跑一到两周**,观察:

- 第几天开始出现异常
- 是否需要重新验证
- 存活率是多少

这个数据决定你能扩到多大规模,也决定你的账号渠道质量。

### 出口不要随机轮换

每个账号首次使用的出口会被自动绑定,之后长期不变。**这是有意的设计,不要去破坏它。**

多个账号随机共用出口、频繁漂移,会让每个账号的来源历史都显得异常。

---

## 出问题了

**先跑自检:**

```powershell
pwsh -File scripts/stack.ps1 doctor
```

| 现象 | 原因 | 解决 |
|---|---|---|
| `找不到网关程序` | 没下载 | `pwsh -File scripts/fetch-gateway.ps1` |
| 下载网关超时 | 直连 GitHub 不通 | 配置 `ProxySeed`,脚本会自动走代理 |
| 网关 502 / `connectex` | 连不上 Google | 检查 `ProxySeed`,确认代理在运行 |
| **502 `cookie 池里 N 个账号都不可用`** | **池里有失效账号,且没降级匿名** | **见下方「失效账号会挡住服务」** |
| `image input needs a Google account cookie` | 池子是空的 | `account.ps1 import-firefox` |
| 传图返回 400 | 没开视觉输入 | Cherry Studio 里编辑模型 → 视觉 = 开 |
| cookie 检测显示"无效" | 过期 / 用了 Chrome | 改用 Firefox 重新登录 |
| 半小时就失效 | Chrome 导出 | **换 Firefox** |
| 端口被占用 | 有别的程序 | `stack.ps1 stop`,或改配置里的端口 |

### 失效账号会挡住服务

**默认配置下,只要 Cookie 池里有账号,网关就一律用它** —— 即使那个账号已经失效,也不会自动降级到匿名模式。池子为空时才会走匿名。

所以一个坏账号能让整个服务报 502:

```
502: cookie 池里 N 个账号都不可用
     (最后一个:no SNlM0e in page (cookie expired or not signed in))
```

两种处理方式:

```powershell
# 方案 A:清掉失效账号(推荐)
pwsh -File scripts/account.ps1 list           # 看哪些失效了
pwsh -File scripts/account.ps1 remove -Id 3   # 删掉

# 方案 B:让服务自动降级匿名
#   管理面板「设置」页 → 打开 fallback_anon
```

**所以定期跑一次这个:**

```powershell
pwsh -File scripts/account.ps1 check-all
```

**还是不行?看日志:**

```powershell
pwsh -File scripts/stack.ps1 logs
```

---

## 目录结构

```
.
├── app/                        Python 业务后端
│   ├── src/image_verifier/     应用源码
│   ├── tests/                  测试
│   └── pyproject.toml
│
├── config/
│   ├── stack.example.psd1      配置模板(带注释)
│   └── stack.psd1              实际配置(自动生成)
│
├── scripts/
│   ├── lib/common.ps1          公共模块
│   ├── install.ps1             一键安装
│   ├── stack.ps1               服务控制
│   ├── account.ps1             账号管理
│   ├── fetch-gateway.ps1       下载网关
│   └── connect-cherry.ps1      注册到 Cherry Studio
│
├── data/gemini.db              网关数据(账号 + 代理)
└── gemini-web2api-go.exe       网关程序(自动下载)
```

**脚本里没有任何硬编码的绝对路径**,整个目录可以随意移动或拷贝到别的机器。

---

## 安全提醒

- `config/stack.psd1` **包含密钥,不要提交到版本库**
- 服务默认只监听 `127.0.0.1`,不对外暴露
- 会话 cookie 等同于账号凭证,等同对待
- 需要对外访问时,自行配置鉴权、TLS 和访问控制

---

## 上游项目

| 项目 | 作用 | 许可 |
|---|---|---|
| [zexadev/gemini-web2api-go](https://github.com/zexadev/gemini-web2api-go) | 资源网关 | MIT |
| [CherryHQ/cherry-studio](https://github.com/CherryHQ/cherry-studio) | 桌面客户端 | Apache-2.0 |

网关程序**不随本仓库分发**,`fetch-gateway.ps1` 会从官方 Releases 下载并校验 SHA256。

---

## 给 AI 助手

如果你打算让 AI 帮你安装,把这个仓库地址丢给它,并告诉它:

> 读 `AGENTS.md`,那里面有完整的安装步骤。

`AGENTS.md` 是专门写给 AI 的 —— 包含环境要求、命令速查、错误对照表、操作原则,以及需要停下来让人类操作的节点。
