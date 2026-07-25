"""Live health-probe backing for the hosted-SaaS observability contract.

See CommunityOverview-SaaS prototypes/observability/{health_checks.yaml,
gcp_monitoring_mappings.yaml} for the checks these probes back (hc-02, hc-03,
hc-07, hc-13). Metric emission and alerting live outside this app, in infra.
"""
