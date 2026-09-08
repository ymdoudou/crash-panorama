# Valuation Tearing and Deep Drawdowns

**Data and code accompanying the paper** · A leading measure and a three-layer
monitor for the AI-economy sectors of the China A-share market

**English** | [中文](#中文说明)

Data through **2026-08-26**; frozen window 2024-01-01 to 2026-08-26,
642 trading days. Snapshot generated 2026-09-08 10:50 (Beijing time).

```
data/   13 datasets   every input behind the paper's results, all taken from the frozen region
code/   12 scripts    how the raw data becomes each indicator
MANIFEST.json         per-file sha256, byte size and source path
```

## What level of reproducibility

This repository supports **verification-level** reproduction: every number in
the paper can be recomputed from `data/`. That is the level review and
replication actually need. One command checks it:

```bash
python3 reproduce.py            # prints paper value / recomputed value / difference
python3 reproduce.py --verbose  # adds the per-fold LOEO detail
```

Standard library only, no third-party dependencies. Exit code 0 when everything
agrees; a ✗ means the paper and the data disagree, and the output should be sent
to the corresponding author.

It does **not** support rebuild-level reproduction, that is, rebuilding every
intermediate quantity from raw quotes. The reason is size and dependency: the
per-stock valuation series, pricing matrix and daily temperature files come to
about 145 MB, the upstream raw quotes are in the gigabytes, and both depend on
the historical availability of third-party data interfaces. The construction
code in `code/` is published as a **readable implementation of the method**, not
as a package that runs end to end.

## Frozen data only, no daily production files

Everything in `data/` comes from the frozen region. The daily production system's
own files are out of scope: they change every day, so publishing them would mean
publishing a dataset that no longer matches the paper. Daily data has its own
public channel (the online dashboard); the two channels stay separate.

Four files are derived by `paper_freeze.py` from daily files clipped to the frozen
window (marked `derived` in MANIFEST); the rest are original sealed files, held
read-only by `manifest.contract`.

One item to note: `p_gap` / `p_d_tech` / `p_d_trad` in `tearing_paper.json` are
expanding-window ranks computed from a 250-day warm-up **before** the frozen
window, and are fixed per row. Do not try to recompute those three columns from
the series in this file; you will get different numbers.

## Thresholds are not in the code

Every threshold of the three-layer system is collected in `data/gate_params.json`,
each with its provenance and calibration method (number of leave-one-episode-out
folds, dispersion across folds, feasible interval). The code reads them through
`code/gate_params.py`, which supports both the flat production layout and the
`data/` + `code/` layout of this package.

The intent is to **separate parameters from implementation**: you can recompute
everything exactly as the paper does, or recalibrate on your own data. What you
get is a method, not a tuned parameter set.

## Names you will meet in the comments

The construction code carries development notes, and a few of them mention parts of
the research system that are not in this package. They are named rather than hidden
because the point they make matters: the valuation ceiling used here is **not built
for this paper**. One `cap` value is computed once and shared by four consumers.

| Name | What it is |
| --- | --- |
| `T` | The system-temperature chain. **This is the chain released here.** |
| `MKR` | A per-stock valuation module that reads the same `cap` |
| `BI` | A breadth indicator built on the same `d` |
| `short_tool` | A short-side screen built on the same series |

Only the `T` chain is released, because that is what the paper's results rest on.
Where a comment says a value is shared with the others, it is stating that the value
was not tuned for one use.

## Relation to the production system

This directory is a **one-way snapshot**: the production system copies into it and
never reads from it. Production keeps evolving; this directory stays at the
version released with the paper. The paper cites one version of the code and one
frozen dataset, not "the latest".

## Not included

Daily pipeline orchestration, message push and data collection scripts are out of
scope: they serve continuous operation and have nothing to do with the
reproducibility of this paper. The same applies to the scripts that generate the
paper page itself, which are typesetting rather than method.

## Authors

**Yumei Dou 窦玉梅** (corresponding author) · yumei.dou@inaicapital.com
**Xinrong Li 李欣嵘** · xinronglee6@gmail.com

Both authors are with **InAI Capital Advisor LLC**, an investment advisory firm
whose research program covers quantitative measurement of A-share market
structure. The three-layer monitor described in the paper runs daily inside the
firm's research system; this repository publishes the frozen slice behind the
paper, together with the code that constructs it.

Contribution statement, funding and competing interests are stated in the paper.

## License

See `LICENSE`. Cite via `CITATION.cff` or the DOI recorded there.

---

<a name="中文说明"></a>

# 估值撕裂与深度下跌

**随文数据集与代码** · A 股 AI 经济板块的前兆度量与三层监测

[English](#valuation-tearing-and-deep-drawdowns) | **中文**

数据截止日 **2026-08-26**；冻结窗口 2024-01-01 ~ 2026-08-26，642 个交易日。
本快照生成于 2026-09-08 10:50（北京时间）。

```
data/   13 个数据集     论文全部结论的输入，全部取自冻结区
code/   12 份构造代码   把数据变成指标的方法
MANIFEST.json          逐文件 sha256、字节数与来源路径
```

## 可复现到哪一级

本仓库支持**验证级**复现：论文里的每一个数字，都能从 `data/` 重算出来。
这也是审稿与复核实际需要的那一级。一条命令即可核对：

```bash
python3 reproduce.py            # 逐项打印「论文值 / 重算值 / 差」
python3 reproduce.py --verbose  # 附带 LOEO 逐折明细
```

只用 Python 标准库，无第三方依赖。全部一致时退出码 0；
出现 ✗ 说明论文与数据对不上，请把输出发给通讯作者。

不支持**重建级**复现，即从原始行情重建全部中间量。原因是体量与依赖：
逐股估值序列、定价矩阵、逐日温度等中间文件合计约 145 MB，
上游原始行情为 GB 级，且依赖第三方数据接口的历史可得性。
`code/` 里的构造代码照发，定位是**方法的可读实现**，不是一键跑通的包。

## 只含冻结数据，不含日频生产文件

`data/` 全部来自冻结区 `frozen/`。日频生产系统自身的文件不在发布范围：
它们每天变化，发出去就是发一份与论文对不上的数据集。
日频数据有独立的公开渠道（在线看板），两条通道各管各的。

其中四个文件由 `paper_freeze.py` 从日频文件截到冻结窗派生（MANIFEST 中标 `derived`），
其余为原始封存件，受 `manifest.contract` 约束只读。

**一处需要注意**：`tearing_paper.json` 里的 `p_gap` / `p_d_tech` / `p_d_trad`
是扩展窗口秩，由冻结窗之前的 250 日暖机期算出，已作为取值固化在每一行。
不要试图从本文件的序列重算这三列，会得到不同的数字。

## 阈值不在代码里

三层系统的全部阈值集中在 `data/gate_params.json`，每一个都附出处与标定方式
（留一事件交叉验证的折数、折间离散度、可行区间等）。代码通过
`code/gate_params.py` 读取，该模块同时支持生产的平铺布局与本包的 `data/` + `code/` 布局。

用意是**参数与实现分离**：读者可以按论文口径完整重算，也可以在自己的数据上
重新标定。拿到的是方法，不是一套调好的参数。

## 注释里会遇到的几个名字

构造代码里带着开发注记，其中几条提到本包之外的研究模块。之所以写出名字而不是
抹掉，是因为那句话本身要紧：这里用的估值天花板**不是为本文特意造的**。
一份 `cap` 只算一次，由四个使用方共用。

| 名字 | 是什么 |
| --- | --- |
| `T` | 系统温度链路。**本包发布的就是这一条。** |
| `MKR` | 逐股估值模块，读同一份 `cap` |
| `BI` | 建立在同一份 `d` 上的广度指标 |
| `short_tool` | 建立在同一批序列上的空头筛选 |

只发布 `T` 这一条，因为论文的结论建立在它之上。注释里说某个值与其余几个共用时，
说的是这个值没有为某一种用途单独调过。

## 与生产系统的关系

本目录是**单向快照**：生产系统拷贝到这里，但从不读取这里。
生产系统持续演进，本目录停在发布时的版本。
论文引用的是某一版代码与某一份数据，不是「当前最新」。

## 未包含

日频流水线编排、消息推送、数据采集等运维脚本不在发布范围：
它们服务于系统的持续运行，与本文的可复现性无关。
论文页面本身的生成脚本同理，属于排版而非方法。

## 作者

**窦玉梅 Yumei Dou**（通讯作者）· yumei.dou@inaicapital.com
**李欣嵘 Xinrong Li** · xinronglee6@gmail.com

两位作者均供职于 **InAI Capital Advisor LLC**，一家投资顾问机构，
其研究方向包含 A 股市场结构的量化度量。论文所述的三层监测系统在该机构的
研究系统中日频运行；本仓库发布的是论文所依据的冻结切片，以及构造它的代码。

作者贡献声明、资助与利益冲突声明见论文正文。

## 许可

见 `LICENSE`。引用方式见 `CITATION.cff`，或使用其中记录的 DOI。
