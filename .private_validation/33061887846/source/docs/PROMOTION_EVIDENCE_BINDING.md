# Promotion evidence code binding

Economic paper promotion needs machine-readable OOS evidence, but the evidence file must itself be storable after the code it evaluates. Requiring that JSON to contain the SHA of the commit that contains the JSON is self-referential and not a producible Git workflow.

The corrected contract treats `source_head_sha` inside promotion evidence as the **bound source-code commit**. The research PR may subsequently add the evidence file or non-economic metadata, producing a descendant research head.

Both Promotion Controller and Integration Merge independently require all of the following:

1. the bound source-code SHA is a valid Git commit;
2. the bound code commit is an ancestor of the current research head;
3. the promotion evidence is fetched from the current research head;
4. the existing objective OOS, cost-stress, drawdown, FDR, stability, incremental-utility, independent-window and data-health gates pass;
5. every economic file that requires source provenance has exactly the same Git blob in:
   - the bound source-code commit;
   - the current research head;
   - the integration candidate;
6. Integration Merge repeats the same checks immediately before squash merge;
7. real-money execution remains outside this authority.

This permits evidence to be committed after a code revision without weakening code provenance. Any economic edit after the bound commit changes a blob and therefore invalidates the candidate until new evidence is generated for the new code commit.
