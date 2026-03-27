# codex-switch

一个零依赖的 Python CLI，用来管理和切换本机 Codex 的 `auth.json` 快照。

它的思路很简单：把不同账号对应的 `~/.codex/auth.json` 保存成多份快照，需要切换时再把目标快照写回当前 live `auth.json`。  
它不伪造登录，也不改 Codex 的服务端状态，只管理你本机上的认证文件。

## 特性

- 基于 Python 3.12 标准库实现，无第三方运行时依赖
- 支持收录当前账号、调用 `codex login` 登录并收录
- 支持列出、查看当前账号、并发查询额度、切换、删除
- 切换前自动备份 live `auth.json`
- 对已纳管账号，`list / current / switch / remove` 前会自动同步最新 token
- 支持通过环境变量覆盖默认目录，便于测试和隔离使用

## 当前范围

- 仅支持 ChatGPT 登录态的 `auth.json`
- 不支持 `OPENAI_API_KEY` 模式
- 不做自动切换或进程管理
- 切换后如果 Codex CLI 或 App 已经在运行，需要你手动重启

## 安装

### 开发模式安装

在项目根目录执行：

```bash
python3 -m pip install -e .
```

安装完成后可直接使用：

```bash
codex-switch --help
```

### 不安装，直接运行

```bash
python3 -m codex_switch --help
```

## 快速开始

### 1. 把当前账号纳入管理

如果你当前的 `~/.codex/auth.json` 已经是想保存的账号：

```bash
codex-switch capture
```

### 2. 登录一个新账号并自动收录

```bash
codex-switch login
```

这个命令会先执行 `codex login`，登录成功后自动把当前账号保存到快照目录。

### 3. 查看已管理账号

```bash
codex-switch list
```

### 4. 查看当前 live 账号

```bash
codex-switch current
```

### 5. 切换到另一个账号

按邮箱精确匹配：

```bash
codex-switch switch someone@example.com
```

按邮箱模糊匹配：

```bash
codex-switch switch someone
```

不传参数时会进入交互选择：

```bash
codex-switch switch
```

### 6. 查看所有已管理账号的额度

```bash
codex-switch usage
```

会按表格显示每个已管理账号的：

- `5H`
- `5H_RESET`
- `WEEKLY`
- `WEEKLY_RESET`

### 7. 删除某个已管理账号

```bash
codex-switch remove someone@example.com
```

同样支持不传参数进入交互选择：

```bash
codex-switch remove
```

## 命令说明

### `capture`

把当前 live `~/.codex/auth.json` 纳入管理。

适合这几种场景：

- 你刚手动执行过 `codex login`
- 你手动恢复或替换了 `~/.codex/auth.json`
- 你想把当前登录态第一次收录进账号库

### `login`

先执行 `codex login`，再自动收录当前账号。

如果你的目标是“登录一个新账号并马上纳管”，优先使用这个命令。

### `list`

列出所有已管理账号，显示：

- `EMAIL`
- `PLAN`
- `SHORT_KEY`
- `FLAGS`

`FLAGS` 中：

- `live` 表示当前 live `auth.json` 对应这个账号
- `active` 表示 registry 记录里的当前活动账号

### `current`

解析当前 live `auth.json`，显示：

- 当前邮箱
- 当前套餐
- 当前短 key
- 当前账号是否已被 `codex-switch` 纳管
- 当前账号是否是 registry 里的活动账号

### `usage`

查询所有已管理账号的额度信息，显示：

- `EMAIL`
- `5H`
- `5H_RESET`
- `WEEKLY`
- `WEEKLY_RESET`

行为规则：

- 默认并发对每个受管账号的快照分别调用 usage API
- 如果当前 live `auth.json` 对应的是某个已管理账号，该账号会优先使用当前 live token 查询
- 单个账号查询失败时不会中断整张表，会在该行显示 `-` 并在表格后追加失败原因
- 如果所有账号都查询失败，命令会返回非零退出码

### `switch [query]`

切换到目标账号。

匹配规则：

1. 先按邮箱精确匹配
2. 再按邮箱模糊匹配

行为规则：

- 不传 `query` 时，如果当前是交互终端，会列出账号供你选择
- 如果命中多个结果且当前不是交互终端，会报错，要求你提供更精确的邮箱
- 切换前若 live `auth.json` 内容将变化，会先在私有目录里创建备份
- 切换后会提示你手动重启已运行的 Codex CLI 或 App

### `remove [query]`

删除已管理账号。

行为规则：

- 匹配方式与 `switch` 相同
- 删除的是受管快照和 registry 条目，不会主动清空当前 live `auth.json`
- 如果删掉的是 registry 当前活动账号，只会清空活动指针，不会改写 live `auth.json`

## 工作原理

### live 认证文件

Codex 当前生效的认证文件：

```text
~/.codex/auth.json
```

### 私有存储目录

`codex-switch` 默认把自己的状态放在：

```text
~/.codex-switcher/
```

其中包括：

- `registry.json`：已管理账号索引
- `accounts/<encoded-record-key>.auth.json`：每个账号一份认证快照
- `backups/auth.json.bak.*`：切换前对 live `auth.json` 的备份

### 账号唯一标识

账号唯一标识不是邮箱，而是：

```text
record_key = chatgpt_user_id::chatgpt_account_id
```

这样即使同一邮箱对应多个不同的 ChatGPT account/workspace，也能区分开。

### 自动同步

对已经纳管的账号，执行下面这些命令前：

- `list`
- `current`
- `switch`
- `remove`

工具会先读取当前 live `auth.json`。  
如果它对应的是一个已管理账号，并且文件内容发生了变化，就会先把最新内容同步回账号快照，避免以后切换回来时用到过期 token。

## 环境变量

- `CODEX_HOME`：覆盖默认 `~/.codex`
- `CODEX_SWITCHER_HOME`：覆盖默认 `~/.codex-switcher`

示例：

```bash
CODEX_HOME=/tmp/codex-demo CODEX_SWITCHER_HOME=/tmp/codex-switcher-demo codex-switch list
```

## 安全提示

这个工具会保存完整的 `auth.json` 快照，其中包含敏感认证信息。请注意：

- 不要把 `~/.codex-switcher` 提交到仓库
- 不要把快照目录共享给不可信用户
- 建议只在你自己的受信任机器上使用

## 开发与测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

查看 CLI 帮助：

```bash
python3 -m codex_switch --help
```

## 常见问题

### 什么时候该用 `capture`？

当“当前 live `auth.json` 已经是你想保存的账号”时，就用 `capture`。

### 日常切换账号时需要手动 `capture` 吗？

不需要。  
如果账号已经纳管，平时直接 `switch` 即可。`capture` 更像是“第一次收录当前账号”。

### 切换后为什么没有马上生效？

因为 `codex-switch` 改的是磁盘上的 `auth.json`，已经运行中的 Codex 进程通常不会自动重新加载。  
切换后请手动重启 Codex CLI 或 App。
