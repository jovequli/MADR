# Ablation protocol

Internal ablation launches use `madr-ablation-v1`. A configuration records both its arm label and its stable implementation identity. The active protocol resolves the label before applying an intervention; unknown labels are rejected.

The release contains 20 nonbaseline configurations: 12 structure, information and procedure ablations, five budget and phase configurations, and three SADR auxiliary controls (SADR-E, SADR-D and SADR-ED). MADR and SADR reference runs are separate and are not included in this count.

S3 uses the independent `single_global_agent_full_vector_v1` protocol. It evaluates one default complete network vector and 169 agent-proposed vectors, without exploration, center rechecks, or an extra final evaluation.

See [catalog.json](catalog.json) for each intervention, evaluation budget and entry point, and [protocol_versions.json](protocol_versions.json) for machine-readable identifiers. The protocol records the experimental definition; it does not change the normal MADR/SADR configuration.
