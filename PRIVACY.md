# Privacy review / 隐私检查

## 结论

当前公开文件经过脱敏，可作为工具源码上传到 GitHub。真实聊天记录与本地验收样本应留在仓库外。

本次检查覆盖源码、测试、文档、Git 候选文件、对象库、忽略项和公开图片。检查时仓库没有提交、没有远端，但对象库留有旧文档的悬空副本；已在仓库外备份 Git 元数据并清理悬空对象。未发现凭证格式命中；发现并处理了个人绝对路径、真实会话标题与 ID，以及本地验收样本。

## 已处理

- 采集器从当前环境解析主目录，移除写死的个人路径。
- 公开文档改用通用路径和技术说明。
- 真实验收文件移至仓库外的私有归档；保留原文件，不改动原始会话。
- 截图中的日期、会话标题与 ID 添加不透明马赛克。
- 新增忽略规则与发布前扫描脚本。
- 测试只使用合成会话和临时文件。

验收结果：19 个 Git 候选文件，扫描命中 0 项；已知个人标识匹配 0 项；本地文档链接全部有效。两张公开 PNG 均不含文本或 EXIF 元数据，像素内容已目视检查。26 项 Python 测试及采集器测试通过。

中文稿机械检查：半角标点、破折号、机械编号标题与空泛套话均为 0 项命中。代码块、命令和 URL 按原样保留。

## 发布范围

可以提交源码、合成测试、README、许可证与已审核的图片。

不要提交聊天导出、来源快照、原始截图、账号文件或私人附件。导出结果保留原文与来源信息，并不是自动脱敏的数据集。发布代码不意味着发布自己的备份。

~~~bash
python3 scripts/check_public_tree.py
git status --short
~~~

扫描脚本读取 Git 候选文件，包括已经被跟踪但后来加入忽略规则的文件。它检查常见凭证格式、个人绝对路径、会话 ID 和私有产物；只打印文件与规则名。它不识别任意自然语言中的个人信息，新增截图须目视检查。首次提交时，按自己的公开身份设置 Git 作者姓名和邮箱。

## English summary

The reviewed public tree is prepared for source-code publication. Private histories and validation artifacts remain outside the repository.

The review covered source, tests, documentation, Git candidates, the object database, ignored artifacts, and public images. The repository had no commits or remote, but unreachable Git objects contained earlier documents. Git metadata was backed up outside the repository before pruning those objects. No credential-pattern matches were found. Personal paths, conversation identifiers, and local validation data were removed from the public tree.

Validation: 19 Git candidates, zero scanner findings, zero known personal-identifier matches, and no broken local documentation links. Both public PNG files have no text or EXIF metadata and were visually reviewed. All 26 Python tests and collector checks passed.

The publication checker scans Git candidates, including tracked files that later became ignored. It reports rule names and file locations without echoing secret values. It does not classify arbitrary personal information in prose or screenshots.

Exports retain original conversation text and source metadata. Keep backups private. Set a suitable public Git author identity before your first commit.
