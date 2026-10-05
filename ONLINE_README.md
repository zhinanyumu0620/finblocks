# FinBlocks 组内网页版部署

网页版源码和部署配置已准备，可使用HTTPS域名、组员邀请码注册、服务端账号/研究历史及管理员分片上传。外部服务器尚未开通，金融数据尚未上传到任何外部平台，没有已经上线的网址。

## 推荐部署入口

[在Render检查并部署本仓库](https://render.com/deploy?repo=https://github.com/zhinanyumu0620/finblocks)

使用账号登录并授权读取本仓库，检查render.yaml中的服务后再确认。**配置使用Starter付费服务与5GB持久化磁盘；费用以平台显示为准，本项目未替用户开通或支付。**免费服务没有持久化磁盘，重启或重新部署会丢失文件及账号，不适合作为这份配置的替代方案。

部署命令仅编译标准库代码并启动Python服务，不执行pip或npm安装。Python版本选择已在本机运行的3.12.14。服务通过Render自动提供的RENDER_EXTERNAL_URL识别实际HTTPS域名，端口读取PORT。

官方说明：[持久化磁盘](https://render.com/docs/disks)、[免费服务限制](https://render.com/docs/free)、[部署入口](https://render.com/docs/deploy-to-render)、[Python版本](https://render.com/docs/python-version)。

## 登录与数据

平台会分别生成FINBLOCKS_INVITE_CODE和FINBLOCKS_ADMIN_CODE。在服务的环境变量页面查看，**只把邀请码给实际组员，管理员上传码不要发给组员或填进仓库**。注册仍只需要昵称和密码，不绑定手机或邮箱。未登录不能访问行情、财报、回测、因子与AI功能；只可浏览公开界面与编译策略。会话Cookie含HttpOnly、SameSite=Strict与Secure。

已有下载数据来自第三方整理材料。目前没有查到其公开再分发授权，因此本版本没有把ZIP传到公开GitHub。管理员上传到组内服务器前，应确认来源许可覆盖团队使用和服务器处理；登录限制不能替代来源授权。服务器不提供原始ZIP下载路由。Github普通仓库单文件超过100MiB被阻断：[官方限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。

## 上传真实金融数据

先取得平台生成的实际HTTPS地址，再在存有原始ZIP的电脑执行以下命令，**把“平台生成的HTTPS地址”替换成真实地址**。不要照抄为虚构域名：

```powershell
python tools/upload_team_data.py --url "平台生成的HTTPS地址" --kind prices --file "股票历史数据-8.19.zip"
python tools/upload_team_data.py --url "平台生成的HTTPS地址" --kind financials --file "财务历史数据/谷票财务历史数据.zip"
```

每次交互输入管理员上传码，不显示输入，也不保存到本地文件。客户端先核对本地字节数及SHA，再按1MiB分片传输；中断可重跑命令从服务器偏移续传。服务端最终再次核验整个文件SHA，只把与已审计原件完全一致的文件启用，不解压或替换原件，不自动造数据。原始两包的大小和SHA来自data/data_upload_spec.json中的实际审计记录。

组员之后仅打开网站、用邀请码注册并登录，无需下载金融数据或安装Python。原始行情的质量门槛保持不变，全部300成员可选不等于任意区间都能通过。

如需腾讯公开研究快照，管理员在平台Shell执行：

```bash
python tools/supplement_public_data.py --root /var/data/finblocks
```

该命令联网下载当前300股快照并保留原响应；必须先上传原始行情以交叉检查单位，不安装依赖，覆盖与阻断以实际响应为准。下载完成后刷新网站。它不是历史基本面补齐，也不是实时行情。

## AI配置（可选）

仅在平台私有环境变量中添加DEEPSEEK_API_KEY，再按平台要求重启服务，不填写到GitHub。网页组员使用服务器配置的这个API账号，会消耗该账号额度；本版本没有为每个组员独立计费或分配模型额度。没有密钥时人工研究可用，不返回假AI。

## 已有服务器

同一代码也可用于已有服务器。设置FINBLOCKS_PUBLIC_ORIGIN为实际HTTPS域名；FINBLOCKS_WORKSPACE为持久化目录；另设独立的组员邀请码、管理员上传码及PORT，运行python serve_online.py。由服务器HTTPS反向代理转发到该端口并保留Host。无需设置或信任用户传来的X-Forwarded-Host。

服务采用单进程标准库HTTP与SQLite，是组内研究原型；没有验收高并发、跨节点会话或生产级服务容量。禁止把源码、私有库、持久化目录或数据目录整体作为静态站点目录公开。

## 验证与状态

本机可以执行 `python -m unittest discover -s tests -p test_deployment.py -v`。测试中的小字节串是上传边界隔离夹具，不作为行情。部署及数据最终状态必须以平台启动成功、管理员上传SHA通过、登录后实际回测及重启后账号/数据仍在为准；本机技术检查不冒充线上验收。
