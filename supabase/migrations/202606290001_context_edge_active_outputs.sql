-- Allow the simplified Context Edge active output set, including NFL Value.
-- Historical output keys remain permitted so existing rows do not block deploys.

alter table public.context_edge_button_outputs
    drop constraint if exists context_edge_button_outputs_output_key_check;

alter table public.context_edge_button_outputs
    add constraint context_edge_button_outputs_output_key_check check (
        output_key in (
            'mlb_value',
            'soccer_value',
            'plus_money',
            'nfl_value',
            'top_5_plays',
            'mlb_lines',
            'world_cup',
            'game_totals'
        )
    );
