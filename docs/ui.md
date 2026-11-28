# Compare two search algorithms in Desktop

Open the example project, run a comparison, and trace one difference in the scores to the ranking that caused it. You need OptPilot Desktop and the public Core checkout containing `catalog/document_search`. The ranking programs run locally. A coding assistant is optional. [Installation](installation.md) · [CLI version of this tutorial](getting-started.md)

## Open the project and review the comparison

1. Choose **Open project** and select the **document_search** folder inside **catalog**. Select the folder containing `optpilot.experiment.yaml`.
2. In **项目关系图** (project graph), choose **设置评测** (set up an evaluation). Check that **Five help questions** is selected as the suite, with both **Word count** and **BM25** selected as candidates.
3. Choose **reciprocal_rank** as the primary metric, with higher values preferred, and use one repetition. Select **Review evaluation**. The review should list five instances, two candidates, local Python execution and **10 planned trials**. Expand **All pinned versions** to inspect the selection. Use **Change selection** if the counts differ.
4. Choose **Start evaluation**. The active evaluation appears above the graph. Open it to follow completion. You can also return to a finished evaluation by double-clicking its node in the graph.

## Explain one difference

Open **概览** (overview) and find **方案评分** (candidate scores). The mean reciprocal ranks are 0.767 for word count and 1.000 for BM25. Switch **显示指标** to **top_one** to see the proportion of first-place hits. The word-count candidate returns the labeled article first on three of five questions. BM25 does so on all five. **试验结果** (trial results) lets you check what happened on a particular question.

Choose the **Isolate changes to files** instance and inspect the evidence from each candidate. Both were asked how to edit files without changing the original. The grader expected the article with ID `folder`. Its position is 3 for word count and 1 for BM25. That explains one of the two extra first-place hits. To evaluate a broader claim about search quality, add new labeled queries and review the expanded suite.

With an active conversation, select **将此试验加入对话草稿** to attach that trial to a draft, or **将评分比较加入对话草稿** from the comparison. You can remove an attachment before sending. Sending it records a link between the experiment evidence and the message, so a reviewer can return to the original conversation.

## Inspect versions and make a revision

- **Follow a result to its source.** Select a version or evaluation in the graph to highlight its immediate relationships. Use **上游** or **下游** to follow a link. Double-click a version for its definition, frozen files and related evaluations. **位置列表** shows linked messages when the project has recorded any.
- **Navigate the graph.** Use **在图中查找** to locate a named artifact or evaluation. Drag the background to pan, click empty space to clear the selection, and use **适应画布** to see the whole graph.
- **Edit a saved candidate.** Choose **开始修改** in the version detail to open a writable draft. After editing, choose **保存为新版本**, inspect the diff and confirm the save. Review a new comparison with the revised candidate and the same suite. The old evaluation still opens its original files and results.
- **Work with an assistant.** A conversation writes to its selected working directory. Reusing a directory shares edits across conversations. **创建独立工作副本** gives a conversation its own copy while keeping results in the same project ledger. Use **附加目录** for other relevant folders.

## Work with other kinds of project

| When the evaluation needs… | Where to go |
| --- | --- |
| A scarce instrument, solver seat or quota | **资源 → 实验资源** shows registered supplies. **设置评测 → 运行计划** shows requirements for this review. Open **查看资源记录** on the evaluation to trace allocations. [Resource tutorial](resource-tutorial.md) |
| A measurement from an operator or a configured SSH worker | Inspect the evaluation's outstanding work or select a registered target when reviewing a native Python study. [Manual measurements](operations.md#submit-a-human-measurement) · [SSH workers](operations.md#use-an-ssh-worker) |
| A Docker-backed agent evaluation or results from another runner | Configure Harbor and its authentication in **设置**, or import an existing report from a reviewed evaluation. [Harbor and external results](external-results.md) |
| A simulator interface or local project tool | Use **项目工具** to open a loopback address or a local HTML file in the selected working directory. To launch a project command, choose its executable and arguments. Closing a view disconnects it. Stopping the tool terminates the process Desktop launched. |

## Set up the workspace for another experiment

Use the **智能体** page to connect a coding assistant and select its provider account. **技能** manages skills available to the assistant. **MCP 服务** controls which tool servers it can use. When returning to a project, select the working directory above the conversation to check which files the assistant can edit. A saved version's **文件** tab opens the snapshot used by earlier runs.

**资源** also lists compute locations, project commands and their job records. Command logs can help diagnose a failed simulator launch. For a candidate's measured score, open the evaluation itself. If it is waiting, read its reason, address the missing supply or measurement and choose **重新检查并继续**. The **关联与记录** tab shows input versions and resource records for the selected run.

**用量** shows recorded model requests and cost estimates. The project view matches requests to retained sessions, so its coverage depends on which logs and pricing are available. Experiment resource reservations and usage receipts are available separately in the run record. Use **设置** for runtime paths and Harbor authentication.

For projects with a graphical environment interface, such as the AGV simulator, open a compatible completed trial to try its public settings in the viewer. You can change those settings to explore a different scenario. To score that change, review and start an evaluation. [Factory scheduling example](llm-policy-search.md)
