# astrbot_plugin_random_pic

AstrBot 随机图片插件。图片按两个正交维度划分：

- **风格**：二次元 / 三次元
- **分级**：全年龄 / 擦边 / R18（R18 仅私聊）

两个维度自由组合，共 6 种。每张图都会附带作者、标题和来源（有来源时）。图源顺序可配置，某个图源失败时自动回退到下一个。

## 指令

```
/随机图 [风格] [分级] [标签...] [数量]
```

| 参数 | 可选值 | 默认 |
|---|---|---|
| 风格 | `二次元` / `三次元` | 二次元 |
| 分级 | `全年龄` / `擦边` / `r18`（也接受 `R18`、`色图`） | 全年龄 |
| 数量 | 数字 | 1，上限由 `max_count` 控制（默认 5） |
| 标签 | 其余所有词 | 无 |

参数顺序随意：先识别风格词和分级词，数字当作数量，其余当作标签。

示例：

```
/随机图
/随机图 擦边 白发 3
/随机图 三次元
/随机图 r18 2          （仅私聊，且需开启 R18）
/随机图 推特 擦边       （Danbooru 推特子模式）
```

便捷别名（`enable_aliases` 开关，默认开启），后面同样可以跟参数：

| 别名 | 等同于 |
|---|---|
| `/二次元` | `/随机图 二次元` |
| `/三次元` | `/随机图 三次元` |
| `/擦边` | `/随机图 擦边` |
| `/色图` | `/随机图 r18` |

回复格式：图片，后面依次附上作者、标题、来源、图站页面和图源名。缺失的字段不显示；既没有来源也没有图站页面时显示“来源未知”。

## 默认路由表

| 风格 \ 分级 | 全年龄 | 擦边 | R18 |
|---|---|---|---|
| 二次元 | nekos_best → waifu_im → danbooru → wallhaven | danbooru → lolicon → wallhaven → yandere | lolicon → danbooru → yandere → waifu_im → wallhaven |
| 三次元 | wallhaven → cn_fallback | wallhaven → cn_fallback | wallhaven |

在 WebUI 的 `routes` 配置中调整各格的顺序；从列表删除就等于关闭该图源。插件加载时会自动剔除不存在的图源、不支持该组合的图源，以及用于 R18 但不返回标签的图源（日志中会有警告）。

## 图源与分级映射

| 图源 | 名称 | 支持组合 | 分级参数 |
|---|---|---|---|
| nekos.best v2 | `nekos_best` | 二次元 × 全年龄 | 分类 neko / waifu / husbando / kitsune，标签只能是这些分类名 |
| waifu.im | `waifu_im` | 二次元 × 全年龄 / R18 | `IsNsfw=False` / `IsNsfw=True` |
| Danbooru | `danbooru` | 二次元 × 全部 | `rating:g` / `rating:s` / `rating:e`（可选 `rating:q,e`） |
| Lolicon API | `lolicon` | 二次元 × 擦边 / R18 | `r18=0` / `r18=1`（r18=0 不保证全年龄，所以不用于全年龄） |
| Wallhaven | `wallhaven` | 二次元 / 三次元 × 全部 | categories `010` / `001`；purity `100` / `010` / `001`（R18 需 API key） |
| yande.re | `yandere` | 二次元 × 全部 | `rating:s` / `rating:q` / `rating:e` |
| Konachan | `konachan` | 二次元 × 全部 | 同 yande.re（默认不在路由中，需要时自行加入） |
| 国内兜底接口 | `cn_fallback` | 三次元 × 全年龄 / 擦边 | 每个 URL 单独标注分级，永远不用于 R18 |

三次元 R18 只接入有审核机制的正规图站（目前只有 Wallhaven），不接入推特搬运、“福利”聚合类或来源不明的接口。

### Danbooru 标签额度（已实测核实）

- 匿名用户和 Member 最多 2 个计数标签；Gold 为 6，Platinum 为 12（配置项 `danbooru.tag_limit`）。
- `rating:` 是免费元标签，不计数。
- `order:random` 和 `random=true` 都计数（后者会被改写为 `random:1`），而且 `order:random` 容易超时，所以插件用 `random=true`。
- `source:` 计数。

因此一次查询的额度分配是：随机排序占 1 个，推特子模式的 `source:*x.com*` / `source:*twitter.com*` 占 1 个，然后是用户标签，最后用剩余额度追加 `-loli -shota`。匿名时如果有用户标签，就没有额度追加负向标签，这时完全依靠本地黑名单过滤。用户标签超出额度时直接跳过 Danbooru，回退到下一个图源。

请求时带自定义 User-Agent（否则会被 Cloudflare 拦截），并限速为每秒最多 10 次。

### 推特子模式

在标签中写 `推特` 进入该模式。只有 Danbooru 支持，其他图源会被跳过。查询时追加 source 条件；可选调用 `api.fxtwitter.com` 补全原推作者和正文（免 key，失败时忽略）。

## 安全机制（硬性，不可通过配置关闭）

1. **R18 双重闸门**：请求阶段先判断是否私聊，群聊一律拒绝 R18；结果阶段再按图源实际返回的 rating 复核，只要结果是 R18 就必须是私聊。R18 总开关 `r18_enabled` 默认关闭；群聊擦边由 `group_sensitive_enabled` 单独控制，默认关闭。
2. **分级复核**：图源实际返回的分级高于请求的分级时（例如请求全年龄却拿到 sensitive / explicit），直接丢弃并重抽。
3. **未成年内容过滤**：所有风格、所有分级都会检查返回的标签，命中黑名单就丢弃并重抽（每个图源最多 `max_retries` 轮，之后回退到下一个图源）。内置基线黑名单不可删除，只能通过 `extra_blacklist` 追加：
   `loli, shota, child, female_child, male_child, toddler, 萝莉, 正太, 幼女, 幼児, ロリ, ショタ`
   - 匹配忽略大小写。英文词按整词匹配（`_`、空格等视为分隔，所以 `loli_bait` 会命中，`lolita_fashion` 不会）；中日文词按子串匹配（`合法ロリ` 会命中）。
   - Lolicon API 没有负向标签参数，完全依靠本地过滤。
   - 不返回标签的图源（nekos.best、国内兜底接口）不能用于 R18；标签为空的 R18 结果也会被丢弃。
4. **访问控制**：群白名单（留空表示所有群可用）、用户黑名单、每人冷却时间和每人每日图片上限（按成功发出的张数计）。黑名单用户和白名单以外的群不会收到任何回复。冷却和每日计数保存在内存中，重启插件后清零。

## 网络与发送

- 支持 HTTP 代理，每个图源可以单独决定是否走代理（`proxy` 配置段）。Pixiv 反代、booru 站和 Wallhaven 在国内通常需要代理。
- 图片先下载到插件数据目录的缓存（`data/plugin_data/astrbot_plugin_random_pic/cache`），再以本地文件发送，不直接发外链。
- 缓存有文件数和总大小上限，每次下载后自动清理最旧的文件。
- 单张图片超过 `cache.max_image_mb` 时自动降级到较小尺寸（Lolicon regular、Danbooru large、Moebooru sample、Wallhaven 缩略图）。
- Lolicon 的原图域名默认 `i.pixiv.re`，可在 `lolicon.proxy_host` 修改；下载 `*.pximg.net` 或所配置反代域名的图片时会自动带上 Pixiv Referer。
- 所有 HTTP 请求都有超时（`request_timeout`）。出现异常时回退到下一个图源；全部失败时回复失败原因，不会没有响应。

## 国内兜底接口

在 `cn_fallback.entries` 中添加条目，每条包括名称、URL 和分级（只能选全年龄或擦边）。URL 必须直接返回图片，或者 302 跳转到图片，例如 `https://v2.api-m.com/api/heisi?return=302`（默认已添加，标为擦边）。这类接口不带标签，所以不能携带标签查询，也不会用于 R18。

## 依赖

- `aiohttp`（见 `requirements.txt`）
- AstrBot ≥ 4.10.4（配置中用到了 `template_list`）
