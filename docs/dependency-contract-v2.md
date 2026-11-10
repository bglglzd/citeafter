# Dependency diagnostics, revision 2

Manual claims always require review. Their recorded evidence includes the selected clause's `kind` and `normative_text`, plus the caller's declared pointers. A declared ancestor already covers its descendants; it does not need duplicate child citations. Additional evidence never proves complete dependency coverage.

A manual explanation lists the declared evidence pointers and the clause's normative-text pointer. Structural `kind` leaves are recorded dependencies but do not themselves explain unproven coverage. An ancestor explanation can cover the normative-text pointer. Missing fields retain their explicit diagnostic pointers.

For every needs-review assessment, `reason_paths` also includes changed recorded dependencies. `changed_required_paths` retains the separate change list, and reverse edges bind those changes to the saved claim/action revision. Unsupported input and manual coverage remain blockers even when every listed value is unchanged.

This revision follows source review of the first frozen implementation. Original development labels, the original held-out labels and its failed first report are preserved. They are historical experiments under revision 1, not new blind measurements of revision 2. The scorer is unchanged. A new freeze and separately authored blind cases are required for a new held-out result.
