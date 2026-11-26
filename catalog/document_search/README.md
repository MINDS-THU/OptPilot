# Search the OptPilot manual

Which ranking puts the right help article first? This small, local search project includes six searchable passages adapted from the OptPilot desktop and evidence guides. Five editorially labeled questions form the evaluation suite. Each candidate receives only the query and the documents and must return a complete ranking.

Word count uses matching word frequencies in the body. BM25 weights rarer words and uses both title and body. The evaluator records the complete ranking, the expected article, its position, a first-place hit (0 or 1) and reciprocal rank (1 / position). Larger values are better. Five questions make it practical to inspect each result individually.

Open this folder in OptPilot. Open evaluation setup from the project map, select the task suite and both candidates, and review the inputs. Run the comparison and attach the result to a conversation to discuss a revision.
