-- Daily Context Edge intelligence reports.
-- One generated report is shared by subscribers for a given date and sport scope.

create extension if not exists pgcrypto;

create table if not exists public.context_edge_daily_reports (
    id uuid primary key default gen_random_uuid(),
    report_date date not null,
    sport_scope text not null default 'all',
    status text not null default 'ready',
    report_json jsonb not null,
    board_hash text not null,
    model text,
    generated_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint context_edge_daily_reports_unique_date_scope
        unique (report_date, sport_scope),
    constraint context_edge_daily_reports_status_check check (
        status in ('pending', 'ready', 'failed', 'stale')
    )
);

create index if not exists context_edge_daily_reports_date_scope_idx
    on public.context_edge_daily_reports (report_date desc, sport_scope);

create index if not exists context_edge_daily_reports_status_idx
    on public.context_edge_daily_reports (status);

alter table public.context_edge_daily_reports enable row level security;

create policy "service role can manage context edge daily reports"
    on public.context_edge_daily_reports
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');
