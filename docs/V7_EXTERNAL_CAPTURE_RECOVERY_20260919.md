# Bounded external-capture recovery

The public-data collector could stay alive after a raw-tape queue overflow.
Its cumulative evidence-invalid flag could never recover in that generation.
The exporter correctly returned 503; changing that gate would hide lost data.

Recovery now archives the exact invalid status before sending SIGTERM to only
that asset's external-data child. Raw tapes are not deleted. A final status is
archived after exit; a new child receives a distinct tape identity. Existing
native execution workers, model, allocation, sizing and PAPER authority are unchanged.

Normal warm-up, stale metadata, disk-pressure suppression and an unavailable
venue without recorded hard evidence loss are not grounds for this restart.
The child has eight seconds to stop. Restarts have a thirty-second cooldown and
at most three recovery requests per five-minute window within this supervisor.
If incident storage fails, the child is not restarted. Exhaustion remains degraded.

Current data readiness can recover, but historical gaps remain invalid. Status
exposes retained incident paths and current-supervisor incident counts. Immutable
incident files survive the supervisor; no claim of recovered frames is made.
