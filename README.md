# 公众号 Agent：把想法做成图文草稿

在 Codex 对话里提供想法或文字，AI 整理文章、调用宿主生图工具生成封面与插图，再由 Python 客户端排版、上传并保存公众号草稿。

**个人主体账号默认交付草稿，由你在微信后台发表。** 本项目在一个个人公众号上实测了连接、上传、草稿创建与更新；该账号正式发布接口返回 48001。支持正式发布接口的账号可显式使用 `--publish`，当前没有完成该路径的真实成功验证。

## 快速开始

需要 Python 3.10+；Windows 可使用 Python 本机版，也可用 WSL。

```bash
python -m pip install -r config/requirements.txt
python src/wechat_publisher.py configure
python src/wechat_publisher.py doctor --online
python scripts/install_skill.py
```

AppID、AppSecret 只在本机交互终端输入。不要粘贴到聊天，也不要提交 `config/account.json`。接口出口 IP 须加入公众号后台的 API IP 白名单。连接成功不代表正式发布权限已开通。

重启/刷新 Codex 的技能列表后，可以说：

> 使用 $wechat-publisher，把下面的想法做成公众号文章，生成封面和插图，排版并保存草稿。

宿主需有写作和生图能力；Python 客户端本身不含语言模型、录音转写或图片生成 API。语音输入需要宿主先转成文字。没有生图工具时不能把占位图片当成真实配图。

## 手动使用

按 `examples/article.json` 准备文章和本地 PNG/JPEG；示例中的图片需要你自己提供。

```bash
python src/wechat_publisher.py build --article examples/article.json --output outputs
python src/wechat_publisher.py run --job "outputs/构建返回的目录"
python src/wechat_publisher.py update-draft --job "新版目录" --previous-job "旧版目录"
```

作者和开放留言通过草稿接口设置，交付前读回草稿核对。`request_original_declaration`、`collection` 是待办元数据，**原创声明和合集须在微信编辑器设置**，本客户端没有自动设置这两项的能力。转载内容不能自动标为原创。

v1.1.1 明确区分“草稿保存”和“编辑器设置完成”：`draft_saved=true` 不能证明合集已选；`editor_settings_complete=false` 表示还有待后台处理的设置。`doctor` 返回默认合集建议，但不会自动加入合集。

文章已经手动发表时，在用户明确确认后记录状态：

```bash
python src/wechat_publisher.py record-manual-publication --job "任务目录" --confirmed-published
```

有正式文章链接时可加 `--article-url "https://mp.weixin.qq.com/s/..."`。此记录的证据来源是用户确认，不是接口发布成功。以后查询不会重新创建或发布该任务。已发表文章漏选合集，应在公众号后台合集管理中添加这篇文章；草稿 ID 无效本身不能证明已经发表。

具备正式发布权限且明确需要发布时：

```bash
python src/wechat_publisher.py set-delivery --mode publish --subject organization
python src/wechat_publisher.py run --job "任务目录" --publish
python src/wechat_publisher.py status --job "任务目录"
```

“organization”配置仅记录账号类型，不会开通任何微信权限。只有微信返回发布状态 0 才报告正式成功；正式发布与订阅者群发是不同操作，本项目不做群发。

## 异常与修订

- 图片失败就停止，禁止缺图提交。
- 同一账号、同一内容复用任务记录，避免重复创建和发布。
- 写入超时或异常中断可能已经执行，必须先核对微信后台；不能删除日志重新提交。
- 草稿修订保留旧文件、远端更新前快照并复用草稿 ID；后台正文已有手动修改时停止覆盖。
- 原创声明与合集必须列在交付待办中，不能把“请求成功”当成已经设置。

日志和配置留在本机；同步或分享文件只复制文章成果。发布客户端不替你判断所有事实或内容权利，仍需核对成稿。

## 测试与来源

```bash
python -m unittest discover -s tests -v
```

本版本有 27 项本地/模拟测试，覆盖上传、权限分流、防重复、未知结果冻结、草稿修订、合集待办和人工发表记录。模拟测试不能代替账号真实接口测试。

流程参考（独立编写客户端，没有复制下列项目的源代码）：

- https://github.com/NimaChu/mp-article
- https://github.com/zhouke0929/wechat-visual-director
- https://github.com/qiuxchao/content-agent

官方接口说明：

- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_update
- https://developers.weixin.qq.com/doc/service/api/public/api_freepublish_submit

MIT License。公开包不含账号凭据、运行日志、私人对话或图片资产。
