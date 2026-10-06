# 个人AI配置与验证

更新日期：2026-10-06。使用现有Python标准库实现，没有安装依赖，没有新增外部收费模型请求。

## 使用方法

1. 保存当前策略后刷新工作台。注册或登录时，在“登录后配置AI（可选）”选择服务商；登录成功后打开个人配置。选择“暂不配置”也能继续使用软件。
2. 已登录用户可随时点击顶部“AI设置”。快捷配置填入API根地址和模型示例，二者都可以编辑。
3. 输入API Key，点击“测试连接”检查短JSON请求。测试单独发送一次请求，可能按账号规则计费，不传金融数据，也不自动保存。点击“保存配置”后启用；保存本身不调用模型。
4. 个人连接用于策略生成、因子候选、回测证据解释和因子解释，依然执行原JSON/DSL及研究边界检查。更换服务商不能保证模型语义正确。

仅支持公开HTTPS、443端口的OpenAI兼容Chat Completions与JSON输出接口；不能直接接原生Anthropic/Gemini协议、聊天网页或本机HTTP模型服务。粘贴以 `/chat/completions` 结尾的接口时会归一化为根地址。

## 快捷配置官方依据

模型ID为可编辑示例，不宣称所有用户均拥有对应权限。千问的旧北京根地址在本轮官方文档中仍可使用，官方同时建议业务空间专属域名；应按账号地域与控制台地址调整。

| 服务商 | 根地址 | 示例模型 | 官方依据 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash` | [接入说明](https://api-docs.deepseek.com/guides/codex)、[JSON输出](https://api-docs.deepseek.com/guides/json_mode/) |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | [模型页](https://developers.openai.com/api/docs/models/gpt-4.1-mini)、[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | [兼容接口](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output) |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-k2.6` | [官方Chat接口](https://platform.kimi.com/docs/api/chat) |

DeepSeek使用禁用思考参数；千问使用非思考参数；Kimi示例K2.6禁用思考；OpenAI不混入其他服务商参数。本轮尚未逐家用用户真实Key测试。

## 账号与密钥

- 元数据保存在 `private/ai_profiles.sqlite3`，请求连接通过ContextVar按线程隔离。页面接口仅返回服务商、地址、模型和配置状态，不回传Key。
- 默认不把Key写入数据库，退出或重新登录后本次Key失效；非秘密配置保留。未设置个人配置时继续使用工作台默认DeepSeek环境配置；个人配置存在但缺Key时明确要求重新填写，不静默借用默认Key。
- Windows可选DPAPI加密记住，数据库只保存密文，同一系统账户下可在重启后解密；其他系统禁用记住选项，不以明文替代。
- 换模型可留空保留同服务商、同地址的Key；换服务商或地址必须重新填写。删除个人配置后恢复工作台默认状态，界面显示实际状态。
- Key不进入localStorage、策略导出或研究历史；输入默认遮挡，关闭设置或切换账号会清空。旧身份返回不能覆盖新身份；保存连接使旧AI候选失效。
- 自定义地址拒绝内网目标、凭据URL及非标准端口；连接固定到已检查的公开IP，保留TLS域名验证，不跟随重定向。错误不回显上游正文；返回包含当前Key的响应会被拒绝。

私有库不进入既有白名单源码包。本轮没有推送新代码到GitHub，没有外部部署。

## 实际验证

```text
Ran 138 tests in 18.249s
OK
tests 31
pass 31
fail 0
skipped 0
```

包含新增11项Python测试与4项前端状态测试：账号隔离、临时Key失效、真实Windows DPAPI加密/重新打开、空Key保留边界、公开地址限制、并发连接隔离、快捷服务商参数、短JSON测试、凭据回显拒绝及旧身份配置返回保护。模型响应是明确TEST_DOUBLE，仅验证协议/逻辑，不属于真实模型成功证据。

日志：[Python](../artifacts/ai_settings_python_tests.txt)、[前端](../artifacts/ai_settings_node_tests.txt)。本机8765服务已重启，页面包含设置按钮及登录服务商选择，新增脚本HTTP 200，公开配置无Key字段。[HTTP检查](../artifacts/ai_settings_live_verification.json)。

完整浏览器点击、视觉及用户真实Key逐家测试待做。有限意图审计、因子经济解释和过拟合算法等既有局限仍按[主体人工核查](core_manual_review.md)处理。参赛提交状态保持NOT_READY。
