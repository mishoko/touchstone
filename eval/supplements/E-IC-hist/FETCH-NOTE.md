# E-IC historical supplement: fetch attempts

**Attempt 1** (code at `74161d2`, the form's commit). It started at unix time 1795164119 and stopped after 9.5 minutes,
near block 25,64x,xxx of 25,139,870–26,443,420. The cross-check of every 50th log chunk queried a second endpoint, and
that endpoint answered "pruned history unavailable" six times in a row. The fetch stopped with an error before
writing any data file. The console showed only cumulative swap counts and timings. No price, path or result was
produced.

**Fix.** The cross-check now queries a third endpoint (`eth.drpc.org`, which returns at most 100 blocks per call) in
100-block pieces and sums them. The count equality stays strict, and the main log fetch is unchanged. The fetch also
asserts that all 14 cross-checks ran (652 chunks, every 50th). Before the restart the check was tested on blocks
25,100,000–25,101,999, outside the window. There, the 20 pieces summed to the main endpoint's count (1,128 logs), and
dropping one piece produced a mismatch.

**Attempt 2** is the one restart. Window, price rule, bots, settings, outputs and the published line are unchanged
(the form's sha256 is unchanged). Any second failure records the supplement as not run.

**Known limits.** The cross-check samples 14 of the 652 chunks. The main fetch rotates between two endpoints. A
pruned node answers with an explicit error, which makes the client retry on the other endpoint.
