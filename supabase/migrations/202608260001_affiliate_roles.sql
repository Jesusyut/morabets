-- Prepare role-based affiliate access without changing customer subscription access.

alter table public.profiles
    add column if not exists role text not null default 'customer';

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'profiles_role_check'
    ) then
        alter table public.profiles
            add constraint profiles_role_check
            check (role in ('customer', 'affiliate', 'admin', 'owner'));
    end if;
end $$;

create table if not exists public.affiliate_profiles (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null unique references public.profiles(id) on delete cascade,
    affiliate_code text not null unique,
    status text not null default 'pending',
    commission_rate numeric(5, 4) not null default 0.3000,
    approved_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint affiliate_profiles_status_check check (
        status in ('pending', 'approved', 'paused', 'rejected')
    ),
    constraint affiliate_profiles_commission_rate_check check (
        commission_rate >= 0 and commission_rate <= 1
    )
);

create index if not exists profiles_role_idx
    on public.profiles (role);

create index if not exists affiliate_profiles_status_idx
    on public.affiliate_profiles (status);

create index if not exists affiliate_profiles_affiliate_code_idx
    on public.affiliate_profiles (affiliate_code);

alter table public.affiliate_profiles enable row level security;

do $$
begin
    if not exists (
        select 1
        from pg_policies
        where schemaname = 'public'
          and tablename = 'affiliate_profiles'
          and policyname = 'service role can manage affiliate profiles'
    ) then
        create policy "service role can manage affiliate profiles"
            on public.affiliate_profiles
            for all
            using (auth.role() = 'service_role')
            with check (auth.role() = 'service_role');
    end if;
end $$;
