# ZipVoice 推理代码（原样复制）

- 来源：<https://github.com/k2-fsa/ZipVoice>，提交 `2f7326fb`（2025-12-02），就是 0-04 实测用的那份
- 许可：Apache-2.0，见同目录 `LICENSE`（同样原样复制）
- 为什么复制：上游没有发包。PyPI 上的 `zipvoice` 0.1.0 只有一个空的 `__init__.py`；仓库本身也没有打包配置，pip / uv 装不了
- 只复制了 TTS 推理用到的 11 个 `.py` 文件，目录结构和上游一致；训练、评测、导出的代码没有复制
- 一个字节都不改，ruff 也不检查这个目录。真要改，先在这里写明改了哪里、为什么
- `pyproject.toml` 是 ARGUS 加的，只为让 uv 把它当本地包装进 gpu 依赖组；依赖照抄上游的 `requirements.txt`

## 核对和上游一致

git 记下的每个文件的哈希（blob SHA）应当和 GitHub 上同一提交的一样：

    git ls-files -s third_party/zipvoice/LICENSE third_party/zipvoice/zipvoice
    curl -s "https://api.github.com/repos/k2-fsa/ZipVoice/git/trees/2f7326fb?recursive=1"

前者的第二列，和后者里同一路径的 `sha` 逐个对比。

## 换上游版本

把这 11 个文件和 `LICENSE` 换成新提交的，改上面的提交号和 `pyproject.toml` 里的版本号，依赖照新的 `requirements.txt` 改；
然后重跑 0-04 那样的实测（首块延迟、回识别准确率），结果记进 `docs/decisions.md` 的 D0-2。
