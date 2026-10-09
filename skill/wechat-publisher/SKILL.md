---
name: wechat-publisher
description: 将想法、笔记或完成稿整理成公众号文章，生成封面与配图，排版并保存草稿；具备权限且用户要求时可正式发布。
---

# 公众号图文交付

先执行本技能 `scripts/publish.py doctor`，读取作者、留言、账号交付模式。未配置时先做文章和预览，让用户在本机终端执行 configure；不能索取 AppSecret 或把密钥放进聊天。

AI 负责写作和使用宿主生图工具；Python 负责确定性排版、上传、草稿、状态与日志。没有独立录音/转写能力。当前事实查证后写入，不杜撰经历或接口能力。生成的插画标明性质，真实截图不得伪造。

按照 examples/article.json 和 references/article-format.md 准备本地 JSON、Markdown 和 PNG/JPEG 图片。默认作者取 doctor，留言开放。用户原创文章需要声明时记 request_original_declaration=true；合集建议写 collection。两项不在官方草稿接口中，须明确列为后台待办，不报告已经完成。

最终文章、图片、预览和回执遵守用户工作区的输出路径要求；构建使用 build --article '<JSON>' --output '<成果目录>'。不能覆盖旧成果，缺图不能继续上传。检查预览后，在用户授权交付的情况下运行 run --job '<目录>'。

个人账号或 publish_api_available=false 时只交付草稿，提示用户到微信后台预览并发表。只有有权限且用户要求正式发布时才传 --publish；状态 0 才返回正式链接。不要调用群发接口。

修订先 build 新版本，再 update-draft --job '<新>' --previous-job '<旧>'，复用草稿 ID。后台已有手动正文改动就停止覆盖。写入结果未知或 *_inflight 必须先核对后台，不删日志、不改指纹、不盲目重试。reconcile 只用于已核实的未知草稿创建/发布 ID；未知草稿更新需核对两版后恢复，不能用创建恢复流程绕过。

交付前通过 draft/get 读回标题、作者、留言、封面、正文图片数；分别报告完成项和原创/合集待办。公开代码仅用源文件白名单，不分享本机配置、日志、备份或用户素材。
