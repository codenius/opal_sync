# ADR 0001: OPAL server state and request concurrency

- Status: Accepted
- Date: 2026-11-20

OPAL requests do not all appear to be independent. We infer that some folder
navigation and Wicket actions depend on page-specific server state, while
ordinary direct-file downloads can run concurrently. We process each Wicket
download before navigating away from its page, but keep regular downloads
concurrent. This is the narrowest ordering rule that fits the evidence; the
evidence does not show that all OPAL requests must be sequential.

## Evidence and inference

| Evidence observed | Inference | Confidence and limit |
| --- | --- | --- |
| During overlapping folder requests, OPAL sometimes returned a parent or sibling listing instead of the requested child listing. A later direct authenticated request to that child returned its listing. | Some folder responses may depend on mutable navigation or page state shared by the authenticated session. | Suggestive, not proof. We did not isolate which concurrent request caused each response. |
| For an E858 PDF, the direct file URL returned a PDF while its Wicket form action returned HTTP 400. A similar result occurred for an E909 PDF. | The Wicket action is not interchangeable with the file URL. The action may need page-specific state that a direct file request does not need. | Observed twice. This does not show that direct file requests never affect OPAL state. |
| Task return-box links use Wicket actions. The action response can direct the client to a Fibercode URL, which returns the file. | Fibercode is a handoff after a page action, not necessarily a stable file URL available before that action. | The response flow was observed; the internal lifetime and scope of a Fibercode URL are not established. |
| A full sync with the older sequential client succeeded. Later concurrent runs showed wrong folder listings and Wicket errors. | Request overlap is a plausible contributor to the failures. | Correlation only. The runs do not prove concurrency was the sole cause. |
| An authenticated direct scan of E909's `Lehrmaterialien (2025)/grundlagen/beispiele` found 60 files without scan errors after the folder URL alias was handled. | At least some apparent missing-folder warnings came from URL-shape differences, not missing OPAL content. | Confirmed for this path and scan; not a rule for every folder. |

## Decision

- Run a Wicket file action as soon as its page exposes it, and finish that action
  before the scraper navigates to another folder or page.
- Keep ordinary direct-file downloads in the bounded worker queue. They can
  overlap with folder discovery, other courses, and a Wicket download.
- Continue course discovery without waiting for regular downloads at each
  course boundary. Wait for queued downloads at the end of the sync.
- Save a course's scanned folder state only after its queued downloads succeed.
  An incomplete scan or failed download preserves the previous folder state.

## Consequences

The Wicket action and the scraper's next navigation remain ordered. Regular
downloads keep using the configured parallel-download limit. This allows
ordinary requests to overlap with a Wicket action on the shared authenticated
client; current evidence does not justify adding a global request lock.

If wrong listings or Wicket errors continue, capture which requests overlap
and compare the Wicket action's page and response before changing the ordering
rule. Do not treat a wrong listing as proof that a folder was removed.
