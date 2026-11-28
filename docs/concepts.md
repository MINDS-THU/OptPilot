# The experiment model

Suppose you replace a document ranker and its score improves. Was the new ranking algorithm better, or did the evaluation questions change? OptPilot lets you vary the candidate while holding the evaluation cases and grading rule fixed. You can then trace a difference in the overall score to the cases that produced it.

## What stays fixed in a comparison?

In the document-search project, the **Task** asks a program to rank a collection of help documents for a query. Its decision contract specifies the kind of submission the Task accepts. Its grader checks the ordering against a held-back target and computes two metrics. The Task can share an **Environment** with other Tasks when they use the same simulator, settings or files.

Each **Instance** supplies a concrete case. The folder question, for example, has a query a ranker may read and a target document the grader uses to score the response. A **Suite** groups saved Instances. Selecting the same Suite gives both rankers the same versions of the questions, targets and Task.

## What can change?

A **Candidate** is one proposed solution. Here, the two Candidates are Python ranking functions. In another project they might be parameter settings for a factory or agent configurations for a Harbor task. A **Generator** is an optional program that proposes Candidates. The Task controls which measured scores it may receive as feedback. You can run a fixed comparison with just the Candidates you supply.

An artifact ID such as `bm25` identifies a candidate across edits. A version ID identifies the saved definition, file contents and dependency versions for a particular revision. Saving a new target for the folder question changes the Instance version and the Suite version that includes it. Earlier results still refer to the original question.

## What did we choose, and what actually ran?

A **Study** selects cases, Candidates, any Generator and evaluation settings such as repetitions. **Review** resolves the selection to exact versions. A **Run** carries out that reviewed Study revision. You can run the same revision again to make a separate set of observations.

A **Trial** measures one Candidate version on one Instance version for one repetition. Two rankers on five questions produce ten Trials for a single repetition. A native **Attempt** records one effort to complete a Trial. A retry creates another Attempt at the same comparison point. A repetition creates a new Trial for each Candidate and case.

The review tells you what the team intended to compare. The Run tells you which inputs reached execution and which measurements came back. Use both when deciding whether to change the ranker. [Run the comparison](getting-started.md) · [Interpret its results](evidence.md)
