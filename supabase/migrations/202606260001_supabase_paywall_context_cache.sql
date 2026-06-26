-- Supabase schema for Mora Bets paywall access and Context Edge response caching.
-- This migration is intentionally additive; it does not affect the existing Flask app yet.

create extension if not exists pgcrypto;

create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    email text not null unique,
    full_name text,
    stripe_customer_id text unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.subscriptions (
    id uuid primary key default gen_random_uuid(),
    profile_id uuid not null references public.profiles(id) on delete cascade,
    stripe_subscription_id text unique,
    stripe_price_id text,
    plan text not null default 'trial',
    status text not null default 'inactive',
    current_period_start timestamptz,
    current_period_end timestamptz,
    trial_ends_at timestamptz,
    cancelled_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint subscriptions_status_check check (
        status in ('inactive', 'trialing', 'active', 'past_due', 'cancelled', 'expired')
    ),
    constraint subscriptions_plan_check check (
        plan in ('trial', 'monthly', 'day_pass', 'admin')
    )
);

create table if not exists public.context_edge_cache (
    id uuid primary key default gen_random_uuid(),
    cache_key text not null unique,
    prompt_hash text not null,
    board_hash text not null,
    user_message text not null,
    response text not null,
    model text,
    sport text,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists profiles_email_idx
    on public.profiles (lower(email));

create index if not exists subscriptions_profile_id_idx
    on public.subscriptions (profile_id);

create index if not exists subscriptions_status_period_idx
    on public.subscriptions (status, current_period_end);

create index if not exists context_edge_cache_cache_key_idx
    on public.context_edge_cache (cache_key);

create index if not exists context_edge_cache_expires_at_idx
    on public.context_edge_cache (expires_at);

alter table public.profiles enable row level security;
alter table public.subscriptions enable row level security;
alter table public.context_edge_cache enable row level security;

create policy "service role can manage profiles"
    on public.profiles
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');

create policy "service role can manage subscriptions"
    on public.subscriptions
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');

create policy "service role can manage context edge cache"
    on public.context_edge_cache
    for all
    using (auth.role() = 'service_role')
    with check (auth.role() = 'service_role');
