-- Cached Context Edge outputs for each existing dashboard quick button.
-- Each output key is generated twice daily and read by all subscribers.

create extension if not exists pgcrypto;

create table if not exists public.context_edge_button_outputs (
    id uuid primary key default gen_random_uuid(),
    report_date date not null,
    run_window text not null,
    output_key text not null,
    status text not null default 'pending',
    report_json jsonb not null,
    board_hash text not null,
    model text,
    error_message text,
    generated_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint context_edge_button_outputs_unique_date_window_key
        unique (report_date, run_window, output_key),
    constraint context_edge_button_outputs_run_window_check check (
        run_window in ('morning', 'afternoon')
    ),
    constraint context_edge_button_outputs_output_key_check check (
        output_key in (
            'mlb_value',
            'soccer_value',
            'top_5_plays',
            'plus_money',
            'mlb_lines',
            'world_cup',
            'game_totals'
        )
    ),
    constraint context_edge_button_outputs_status_check check (
        status in ('pending', 'ready', 'failed', 'stale')
    )
);

create index if not exists context_edge_button_outputs_date_window_key_idx
    on public.context_edge_button_outputs (report_date desc, run_window, output_key);

create index if not exists context_edge_button_outputs_key_status_generated_idx
    on public.context_edge_button_outputs (output_key, status, generated_at desc);

create index if not exists context_edge_button_outputs_status_idx
    on public.context_edge_button_outputs (status);

alter table public.context_edge_button_outputs enable row level security;

create policy "service role can manage context edge button outputs"
    on public.context_edge_button_outputs
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');
