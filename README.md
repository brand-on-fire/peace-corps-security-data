# Public Peace Corps country hazard observations

This repository maintains public-source observations for reviewed active Peace Corps countries. It is an independent policy research dataset, not an official Peace Corps service or a determination that a place is safe.

`docs/data/snapshot.json` contains the current 30-day view. `docs/data/history.json` is a selected-field index; complete reports and revisions are in the country files under `docs/data/history/`. Country manifests may list multiple complete-record parts. No Volunteer names, private locations or postings are collected.

`docs/data/discovery.json` contains keyword-matched RSS titles and links awaiting review. These are not map incidents. A publisher's country, publication time, or a place mention does not establish the event's location, date, or truth. Full news bodies are not republished.

The workflow runs only in the public `brand-on-fire/peace-corps-security-data` repository on its default branch. Its initial authorized push and subsequent collector/configuration changes start a run. It uses standard Ubuntu runners, the built-in ephemeral repository token, public HTTP feeds and no Actions caches or artifacts. It requests an official-feed check every 15 minutes and RSS every six hours. GitHub schedules can be delayed or dropped. Source health and dates are part of the data.

Country admission expires at the documented review date. A country needs a renewed, evidence-backed roster review to resume new observations. Current coverage excludes offshore hazard effects, does not establish every local-news source, and is not an emergency alert channel. Read each observation's source, precision, evidence status and dates.

The collector stops at explicit data-tree and repository-history growth limits rather than deleting the archive or using paid storage. Publication uses ordinary public Git files, readable at raw.githubusercontent.com. GitHub Pages is not enabled by this workflow.

## Collection health and scheduling

Collection runs in an isolated staging directory with an eight-minute deadline. Only a complete validated archive and snapshot replaces the last good publication; validation checks archived record identities, retained evidence, active-country scope, archive totals, and the browser content revision. A fatal failure or failure of every due source preserves the prior publication and writes only `docs/data/refresh-status.json`. Partial source outages retain prior records and are reported as degraded.

The status document records the GitHub run URL, trigger, attempt, source outcomes, last successful collection time, and most recent observed scheduled run. A successful push or manual run is never recorded as proof of natural scheduling. The workflow publishes a failed-run diagnostic before marking the run failed; preflight, test, or publication failures remain visible in Actions. There are no external scheduler, model, cloud runtime, cache, or artifact dependencies.

The requested schedule is minutes 11, 26, 41 and 56 each UTC hour; official feeds are due once per 15-minute slot and news discovery once per six-hour slot. GitHub may delay or drop scheduled runs, so this is not a guaranteed live emergency-warning service. Inspect the source success timestamps and the public workflow run history, rather than interpreting a snapshot timestamp as proof every source is fresh.
