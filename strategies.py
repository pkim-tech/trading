import pandas as pd


class BaseStrategy:
    """sl_axis/fourth_axis/uses_fixed_sl: single source of truth for what the swept
    grid columns actually mean per strategy (see docs/design.md 'Grid axis meaning
    by strategy'). Consulted by resolve_axis_columns()/uses_fixed_sl() below instead
    of each caller keeping its own issubclass chain in sync."""
    sl_axis = 'stop_loss'   # real backtest_cache column the swept 'sl' grid value populates
    fourth_axis = None      # extra swept axis name, or None if the strategy doesn't have one
    uses_fixed_sl = False   # real SL comes from config.execution.fixed_stop_loss, not a grid axis
    # params_take_profit_key (2026-09-10, promote_candidate.py generalization): the real
    # candidate_nodes.params_json key holding this strategy's "take_profit arg" concept
    # signals_db.add_node() expects -- 'take_profit' for most strategies, but the
    # TrailingBuy/TrailingBoth family calls the same concept 'arm_pct' in params_json
    # (confirmed against real rows: candidate_nodes id 49622/38604 both TB, key is
    # 'arm_pct'; id 50073 TE, key is 'take_profit'). add_node() itself already knows to
    # store this into arm_sell_pct instead of take_profit for TrailingBothZScoreBreakout
    # specifically -- this attribute is a DIFFERENT, earlier step (which params_json key
    # to READ), single source of truth so a promotion tool doesn't have to guess via
    # dict.get() fallback ordering.
    params_take_profit_key = 'take_profit'
    # uses_arm_trail_exit (2026-09-02, drought-overlay generalization, see backtester.
    # simulate_drought_overlay_ground_truth's own docstring): True only for a strategy that
    # is BOTH (a) shaped like the fixed-SL-then-arm-then-trail state machine the drought
    # overlay reuses to manage its own position (scripts/drought_overlay_test.py's
    # simulate_overlay) AND (b) an actually GT-kernel-supported strategy (backtester.py's
    # own is_both-scoped GT kernel only knows TrailingBoth/TrailingExit -- see its GT scope
    # note). Do NOT flip this to True off shape alone: LimitOrderTrailingExit's check_exit
    # is ALSO byte-identical (confirmed via ast.dump) but stays False here because the GT
    # kernel doesn't support it at all -- shape match without kernel support would still be
    # meaningless (there's no real GT trade list to run the overlay against in the first
    # place). A capability flag here (same pattern as sl_axis/fourth_axis above) instead of
    # a hardcoded strategy-name set at each call site -- paired-review finding: a future
    # strategy paradigm (plain TP, time-based, vol-target exit) would otherwise silently
    # get a plausible-looking but meaningless drought number computed under an exit model
    # its own core never uses, with no error to catch it.
    uses_arm_trail_exit = False

    def __init__(self, **kwargs):
        self.params = kwargs

    def generate_daily_indicators(self, df_daily):
        raise NotImplementedError

    def check_signal(self, ctx):
        raise NotImplementedError

    def check_exit(self, ctx):
        raise NotImplementedError


def _entry_price_field(ctx):
    """Close for bar-close entry strategies, Low for intrabar-touch entry strategies."""
    return ctx['current_price']


def resolve_axis_columns(strategy_name):
    """(sl_axis_column, fourth_axis_column_or_None) for this strategy — single source
    of truth, see BaseStrategy.sl_axis/fourth_axis."""
    cls = globals().get(strategy_name)
    if cls is None or not issubclass(cls, BaseStrategy):
        return 'stop_loss', None
    return cls.sl_axis, cls.fourth_axis


def params_json_take_profit_key(strategy_name):
    """The real candidate_nodes.params_json key holding this strategy's take_profit-arg
    concept for signals_db.add_node() -- see BaseStrategy.params_take_profit_key's own
    comment. Same fallback-to-BaseStrategy pattern as resolve_axis_columns() above."""
    cls = globals().get(strategy_name)
    if cls is None or not issubclass(cls, BaseStrategy):
        return 'take_profit'
    return cls.params_take_profit_key


def uses_fixed_sl(strategy_name):
    """Whether this strategy's SL is a fixed (non-swept) value rather than a swept
    grid axis — see BaseStrategy.uses_fixed_sl. config.execution.fixed_stop_loss is
    only a default used during backtesting; signals_db.add_node() requires the real
    per-node SL to be passed explicitly via fixed_sl_override when creating a node
    for a strategy where this returns True, rather than silently trusting that
    config default."""
    cls = globals().get(strategy_name)
    return cls is not None and issubclass(cls, BaseStrategy) and cls.uses_fixed_sl


def uses_arm_trail_exit(strategy_name):
    """Whether this strategy's check_exit is the fixed-SL-then-arm-then-trail state
    machine the drought overlay reuses to manage its own position (scripts.
    drought_overlay_test.simulate_overlay) -- see BaseStrategy.uses_arm_trail_exit's own
    comment. A caller computing a drought overlay CAGR should gate on this, not a
    hardcoded strategy-name check, so a future strategy paradigm with a genuinely
    different exit shape doesn't silently get a plausible-looking but meaningless
    drought number computed under an exit model its own core never uses."""
    cls = globals().get(strategy_name)
    return cls is not None and issubclass(cls, BaseStrategy) and cls.uses_arm_trail_exit


def validate_axis_values(strategy_name, trail_buy_pct=None, trail_pct=None):
    """Warn (not raise) if trail_buy_pct/trail_pct are being set for a strategy that
    doesn't use that axis, or left unset for one that does — catches the class of bug
    that put wrong values in watch_list silently for days (see docs/backlog.md,
    2026-07-05 add_node() trail_buy_pct/trail_pct mis-mapping)."""
    sl_axis, fourth_axis = resolve_axis_columns(strategy_name)
    uses_trail_buy_pct = sl_axis == 'trail_buy_pct'
    uses_trail_pct = sl_axis == 'trail_pct' or fourth_axis == 'trail_pct'

    warnings = []
    if not uses_trail_buy_pct and trail_buy_pct:
        warnings.append(f"{strategy_name} doesn't use trail_buy_pct (bar-close/fixed-SL "
                         f"entry) — value {trail_buy_pct} will have no effect")
    if uses_trail_buy_pct and not trail_buy_pct:
        warnings.append(f"{strategy_name} requires trail_buy_pct but none/zero was given")
    if not uses_trail_pct and trail_pct:
        warnings.append(f"{strategy_name} doesn't use trail_pct — value {trail_pct} "
                         f"will have no effect")
    if uses_trail_pct and not trail_pct:
        warnings.append(f"{strategy_name} requires trail_pct but none/zero was given")
    return warnings


def validate_row_axis_mapping(strategy_name, row_take_profit, row_stop_loss, row_trail_buy_pct,
                               row_trail_pct, row_arm_sell_pct):
    """Raises ValueError if a freshly-computed backtest_cache/backtest_phase1 row's
    overloaded columns don't match what this strategy actually declares (sl_axis/
    fourth_axis, see resolve_axis_columns() above) -- only the column(s) a strategy
    owns may hold a non-neutral value; every other overloaded column must stay at
    its documented neutral default (0/0.0 for trail_buy_pct/trail_pct, None for
    take_profit XOR arm_sell_pct). Targets the recurring "column meant something
    else" bug family (4+ confirmed real instances -- take_profit vs arm_sell_pct vs
    drought-arm-override, trail_buy_pct/trail_pct mis-mapping -- see
    docs/backlog_cache.md's 2026-08-07/2026-08-22 entries and
    docs/plans/backtest_schema_v2_phase_tables.md). Call this at write time, right
    before a row is persisted -- a violation here means the CALLER's own axis-to-
    column mapping logic has a bug, not that the input data is questionable (contrast
    with validate_axis_values() above, which warns rather than raises for exactly
    that reason -- this function's inputs are internally computed, not user-supplied).

    Deliberately does not check stop_loss's fixed_sl-mirror behavior (uses_fixed_sl
    strategies store round(fixed_sl) in stop_loss, not a swept axis value) -- that's
    a real, separate mapping rule, out of scope for this first version."""
    sl_axis_col, fourth_axis_col = resolve_axis_columns(strategy_name)
    owns_trail_buy_pct = 'trail_buy_pct' in (sl_axis_col, fourth_axis_col)
    owns_trail_pct = 'trail_pct' in (sl_axis_col, fourth_axis_col)
    violations = []

    if not owns_trail_buy_pct and row_trail_buy_pct:
        violations.append(f"trail_buy_pct={row_trail_buy_pct!r} set but {strategy_name} doesn't "
                           f"own that column (sl_axis={sl_axis_col!r}, fourth_axis={fourth_axis_col!r})")
    if owns_trail_buy_pct and not row_trail_buy_pct:
        violations.append(f"{strategy_name} owns trail_buy_pct but got {row_trail_buy_pct!r}")

    if not owns_trail_pct and row_trail_pct:
        violations.append(f"trail_pct={row_trail_pct!r} set but {strategy_name} doesn't own "
                           f"that column (sl_axis={sl_axis_col!r}, fourth_axis={fourth_axis_col!r})")
    if owns_trail_pct and not row_trail_pct:
        violations.append(f"{strategy_name} owns trail_pct but got {row_trail_pct!r}")

    # take_profit/arm_sell_pct: TrailingBothZScoreBreakout stores its swept 'tp' grid
    # value in arm_sell_pct (take_profit NULL, since backtest_cache's composite PK
    # can't dedupe on a column that's sometimes NULL); every other strategy is the
    # reverse. See init_idempotent_db()'s axis_tp/take_profit split comment.
    if strategy_name == 'TrailingBothZScoreBreakout':
        if row_take_profit is not None:
            violations.append(f"take_profit={row_take_profit!r} should be None for "
                               f"TrailingBothZScoreBreakout (value belongs in arm_sell_pct)")
        if row_arm_sell_pct is None:
            violations.append("arm_sell_pct is None for TrailingBothZScoreBreakout "
                               "(should hold the swept take-profit value)")
    else:
        if row_arm_sell_pct is not None:
            violations.append(f"arm_sell_pct={row_arm_sell_pct!r} should be None for "
                               f"{strategy_name} (only TrailingBothZScoreBreakout uses it)")
        if row_take_profit is None:
            violations.append(f"take_profit is None for {strategy_name} "
                               f"(should hold the swept take-profit value)")

    if violations:
        raise ValueError(f"validate_row_axis_mapping({strategy_name}): " + "; ".join(violations))


class ZScoreBreakout(BaseStrategy):
    """v1.5/v1.6: bar-close entry, bar-close TP/SL/TIME. Mirrors backtester._simulate."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        # backtester._simulate: TP, then SL, then TIME — bar-close only, every check.
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        cp, ep = ctx['current_price'], ctx['entry_price']
        pc = (cp - ep) / ep
        if pc >= ctx['take_profit']:
            return 'TP', cp, ctx.get('state', {})
        if pc <= -ctx['stop_loss']:
            return 'SL', cp, ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', cp, ctx.get('state', {})
        return None, None, ctx.get('state', {})


class TrailingExitZScoreBreakout(BaseStrategy):
    """v1.8: bar-close entry. SL + trailing-stop are intrabar (continuous);
    TP-activation and TIME (pre-activation) are bar-close. Mirrors backtester._simulate_trail."""
    sl_axis = 'trail_pct'
    uses_fixed_sl = True
    uses_arm_trail_exit = True  # confirmed byte-identical to TrailingBoth's own check_exit (ast.dump)

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        state = dict(ctx.get('state', {}))
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        trail_pct  = self.params.get('trail_pct', 0.03)
        op = ctx.get('open', ctx['current_price'])

        if state.get('trailing'):
            prior_peak = state.get('peak', ep)
            trail_stop_gap = prior_peak * (1 - trail_pct)
            # Open-first gap check -- mirrors the exit-side gap-through-trigger fix
            # in backtester.py (2026-07-20): if the bar's Open already cleared the
            # trailing-stop confirmed through the prior bar, that's the honest fill,
            # not the theoretical trail_stop level (which the position never
            # actually traded at).
            if op <= trail_stop_gap:
                reason = 'WIN' if op > ep else 'LOSS'
                return reason, op, state
            # Compute the running peak for THIS check -- still needed so a
            # mid-bar poll can detect a genuine breach in real time, not just
            # at bar close (in practice, live's mid-bar caller passes
            # high == low == current_price, so this mainly matters for
            # comparing against `prior_peak`; a real resting broker
            # TRAILING_STOP order is the actual continuous tracker, this is
            # just local bookkeeping to notice a breach before the next bar
            # closes) -- but only persist it into `state` at bar close. The
            # 2026-07-20 gap-through fix depends on
            # `prior_peak` reflecting only what was confirmed through the
            # PRIOR closed bar; a mid-bar poll writing this bar's own
            # still-forming high into state['peak'] would corrupt that
            # invariant, so the next bar-close gap check compares its Open
            # against a trail level that only existed because of this same
            # bar's own intrabar movement -- incoherent, since the Open
            # occurs before that movement happened (found live via
            # execution-path walkthrough, 2026-07-31).
            peak = max(prior_peak, ctx['high'])
            trail_stop = peak * (1 - trail_pct)
            if ctx.get('at_bar_close', True):
                state['peak'] = peak
            if ctx['low'] <= trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']:
                exit_px = trail_stop if ctx['low'] <= trail_stop else ctx['current_price']
                reason = 'WIN' if exit_px > ep else 'LOSS'
                # Distinguishes a genuine trail-stop breach from hold-time
                # expiring while still armed -- both collapse to the same
                # WIN/LOSS reason (kept as-is for backtest parity with
                # backtester.py's identical dual-condition check), but live
                # order execution needs to tell them apart: a genuine breach
                # means a resting trailing-sell order is already correctly
                # tracking this and should just be polled for its fill; a
                # hold-time-forced exit means that resting order is still far
                # from its trigger and must be force-replaced with a market
                # sell instead (found live 2026-07-29, SH: held 28h/24h while
                # armed with a 50%-wide trail, stuck waiting on an order that
                # was nowhere near firing). state is live-code-only
                # bookkeeping, never consumed by the numba kernel, so this
                # carries zero backtest blast radius.
                if ctx['low'] > trail_stop:
                    state['exit_forced_by_hold_time'] = True
                return reason, exit_px, state
            return None, None, state

        # Same Open-first gap check on the fixed SL.
        if op <= stop_price:
            return 'SL', op, state
        if ctx['low'] <= stop_price:
            return 'SL', stop_price, state

        if not ctx.get('at_bar_close', True):
            return None, None, state

        if ctx['current_price'] >= tp_price:
            state['trailing'] = True
            state['peak'] = ctx['current_price']
            return None, None, state

        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], state

        return None, None, state


class LimitOrderZScoreBreakout(BaseStrategy):
    """v1.7: intrabar-touch entry (Low vs band). SL is intrabar (continuous);
    TP and TIME are bar-close. Mirrors backtester._simulate_limit."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        # Entry uses Low (intrabar touch), not Close — differs from the other strategies.
        return 'BUY' if ctx['low'] <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        # Note: for v1.7, entry_price passed in is the fill price (lower_band at entry),
        # not necessarily the live price at signal time.
        ep = ctx['entry_price']
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])

        if ctx['low'] <= stop_price:
            return 'SL', stop_price, ctx.get('state', {})

        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})

        if ctx['current_price'] >= tp_price:
            return 'TP', ctx['current_price'], ctx.get('state', {})

        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], ctx.get('state', {})

        return None, None, ctx.get('state', {})


class LimitOrderTrailingExit(LimitOrderZScoreBreakout):
    """v2.11: v1.7's intrabar-touch entry (Low vs band, fill at band price) combined with
    v1.8's trailing-stop exit instead of fixed TP/SL. Mirrors backtester._simulate_limit_trail."""
    sl_axis = 'trail_pct'
    uses_fixed_sl = True

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        state = dict(ctx.get('state', {}))
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        trail_pct  = self.params.get('trail_pct', 0.03)
        op = ctx.get('open', ctx['current_price'])

        if state.get('trailing'):
            prior_peak = state.get('peak', ep)
            trail_stop_gap = prior_peak * (1 - trail_pct)
            # Open-first gap check -- mirrors the exit-side gap-through-trigger fix
            # in backtester.py (2026-07-20): if the bar's Open already cleared the
            # trailing-stop confirmed through the prior bar, that's the honest fill,
            # not the theoretical trail_stop level (which the position never
            # actually traded at).
            if op <= trail_stop_gap:
                reason = 'WIN' if op > ep else 'LOSS'
                return reason, op, state
            # Compute the running peak for THIS check -- still needed so a
            # mid-bar poll can detect a genuine breach in real time, not just
            # at bar close (in practice, live's mid-bar caller passes
            # high == low == current_price, so this mainly matters for
            # comparing against `prior_peak`; a real resting broker
            # TRAILING_STOP order is the actual continuous tracker, this is
            # just local bookkeeping to notice a breach before the next bar
            # closes) -- but only persist it into `state` at bar close. The
            # 2026-07-20 gap-through fix depends on
            # `prior_peak` reflecting only what was confirmed through the
            # PRIOR closed bar; a mid-bar poll writing this bar's own
            # still-forming high into state['peak'] would corrupt that
            # invariant, so the next bar-close gap check compares its Open
            # against a trail level that only existed because of this same
            # bar's own intrabar movement -- incoherent, since the Open
            # occurs before that movement happened (found live via
            # execution-path walkthrough, 2026-07-31).
            peak = max(prior_peak, ctx['high'])
            trail_stop = peak * (1 - trail_pct)
            if ctx.get('at_bar_close', True):
                state['peak'] = peak
            if ctx['low'] <= trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']:
                exit_px = trail_stop if ctx['low'] <= trail_stop else ctx['current_price']
                reason = 'WIN' if exit_px > ep else 'LOSS'
                # Distinguishes a genuine trail-stop breach from hold-time
                # expiring while still armed -- both collapse to the same
                # WIN/LOSS reason (kept as-is for backtest parity with
                # backtester.py's identical dual-condition check), but live
                # order execution needs to tell them apart: a genuine breach
                # means a resting trailing-sell order is already correctly
                # tracking this and should just be polled for its fill; a
                # hold-time-forced exit means that resting order is still far
                # from its trigger and must be force-replaced with a market
                # sell instead (found live 2026-07-29, SH: held 28h/24h while
                # armed with a 50%-wide trail, stuck waiting on an order that
                # was nowhere near firing). state is live-code-only
                # bookkeeping, never consumed by the numba kernel, so this
                # carries zero backtest blast radius.
                if ctx['low'] > trail_stop:
                    state['exit_forced_by_hold_time'] = True
                return reason, exit_px, state
            return None, None, state

        # Same Open-first gap check on the fixed SL.
        if op <= stop_price:
            return 'SL', op, state
        if ctx['low'] <= stop_price:
            return 'SL', stop_price, state
        if not ctx.get('at_bar_close', True):
            return None, None, state
        if ctx['current_price'] >= tp_price:
            state['trailing'] = True
            state['peak'] = ctx['current_price']
            return None, None, state
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], state
        return None, None, state


class TrailingBuyZScoreBreakout(BaseStrategy):
    """v1.9: after z-score signal, waits for price to bounce trail_buy_pct% above running low before entering.
    Exit: fixed TP (bar-close) + fixed SL (intrabar) + hold cap. Mirrors backtester._simulate_trail_buy."""
    sl_axis = 'trail_buy_pct'
    uses_fixed_sl = True

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        if ctx['low'] <= stop_price:
            return 'SL', stop_price, ctx.get('state', {})
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        if ctx['current_price'] >= tp_price:
            return 'TP', ctx['current_price'], ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], ctx.get('state', {})
        return None, None, ctx.get('state', {})


class TrailingBothZScoreBreakout(TrailingBuyZScoreBreakout):
    """v1.10: trailing entry (bounce above running low) + trailing exit once TP% cleared.
    Mirrors backtester._simulate_trail_both."""
    fourth_axis = 'trail_pct'
    uses_arm_trail_exit = True  # the original strategy this exit machine was designed for
    # params_take_profit_key='arm_pct' (2026-09-10, paired-review MEDIUM fix): placed
    # HERE, not on the parent TrailingBuyZScoreBreakout -- confirmed against the real
    # params_json writer (scripts/node_key.py's build_params_dict), which special-cases
    # this key ONLY for strategy_name == 'TrailingBothZScoreBreakout' by exact name.
    # TrailingBuyZScoreBreakout's own check_exit (this class overrides it below) is a
    # genuine fixed-TP exit, not the arm/trail machine -- its real params_json key is
    # 'take_profit' like any other non-arm/trail strategy, inherited correctly from
    # BaseStrategy's own default. Putting the override one class too high was the
    # independent-cold reviewer's real, confirmed finding on this diff's first version.
    params_take_profit_key = 'arm_pct'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        state = dict(ctx.get('state', {}))
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])
        trail_pct  = self.params.get('trail_pct', 0.03)
        op = ctx.get('open', ctx['current_price'])

        if state.get('trailing'):
            prior_peak = state.get('peak', ep)
            trail_stop_gap = prior_peak * (1 - trail_pct)
            # Open-first gap check -- mirrors the exit-side gap-through-trigger fix
            # in backtester.py (2026-07-20): if the bar's Open already cleared the
            # trailing-stop confirmed through the prior bar, that's the honest fill,
            # not the theoretical trail_stop level (which the position never
            # actually traded at).
            if op <= trail_stop_gap:
                reason = 'WIN' if op > ep else 'LOSS'
                return reason, op, state
            # Compute the running peak for THIS check -- still needed so a
            # mid-bar poll can detect a genuine breach in real time, not just
            # at bar close (in practice, live's mid-bar caller passes
            # high == low == current_price, so this mainly matters for
            # comparing against `prior_peak`; a real resting broker
            # TRAILING_STOP order is the actual continuous tracker, this is
            # just local bookkeeping to notice a breach before the next bar
            # closes) -- but only persist it into `state` at bar close. The
            # 2026-07-20 gap-through fix depends on
            # `prior_peak` reflecting only what was confirmed through the
            # PRIOR closed bar; a mid-bar poll writing this bar's own
            # still-forming high into state['peak'] would corrupt that
            # invariant, so the next bar-close gap check compares its Open
            # against a trail level that only existed because of this same
            # bar's own intrabar movement -- incoherent, since the Open
            # occurs before that movement happened (found live via
            # execution-path walkthrough, 2026-07-31).
            peak = max(prior_peak, ctx['high'])
            trail_stop = peak * (1 - trail_pct)
            if ctx.get('at_bar_close', True):
                state['peak'] = peak
            if ctx['low'] <= trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']:
                exit_px = trail_stop if ctx['low'] <= trail_stop else ctx['current_price']
                reason = 'WIN' if exit_px > ep else 'LOSS'
                # Distinguishes a genuine trail-stop breach from hold-time
                # expiring while still armed -- both collapse to the same
                # WIN/LOSS reason (kept as-is for backtest parity with
                # backtester.py's identical dual-condition check), but live
                # order execution needs to tell them apart: a genuine breach
                # means a resting trailing-sell order is already correctly
                # tracking this and should just be polled for its fill; a
                # hold-time-forced exit means that resting order is still far
                # from its trigger and must be force-replaced with a market
                # sell instead (found live 2026-07-29, SH: held 28h/24h while
                # armed with a 50%-wide trail, stuck waiting on an order that
                # was nowhere near firing). state is live-code-only
                # bookkeeping, never consumed by the numba kernel, so this
                # carries zero backtest blast radius.
                if ctx['low'] > trail_stop:
                    state['exit_forced_by_hold_time'] = True
                return reason, exit_px, state
            return None, None, state

        # Same Open-first gap check on the fixed SL.
        if op <= stop_price:
            return 'SL', op, state
        if ctx['low'] <= stop_price:
            return 'SL', stop_price, state
        if not ctx.get('at_bar_close', True):
            return None, None, state
        if ctx['current_price'] >= tp_price:
            state['trailing'] = True
            state['peak'] = ctx['current_price']
            return None, None, state
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], state
        return None, None, state


class LimitExitZScoreBreakout(BaseStrategy):
    """v2.12: bar-close confirmed entry (like ZScoreBreakout). SL is a fixed intrabar floor.
    TP is a resting limit order — fills intrabar the moment High touches tp_price, at tp_price
    (guaranteed, no waiting for bar-close). Mirrors backtester._simulate_close_limitexit."""

    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        return df[['SMA', 'Std']].dropna()

    def check_signal(self, ctx):
        sma, std = ctx['sma'], ctx['std']
        if std == 0 or pd.isna(sma) or pd.isna(std):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        return 'BUY' if _entry_price_field(ctx) <= sma - std * threshold else 'HOLD'

    def check_exit(self, ctx):
        ep = ctx['entry_price']
        stop_price = ep * (1 - ctx['stop_loss'])
        tp_price   = ep * (1 + ctx['take_profit'])

        if ctx['low'] <= stop_price:
            return 'SL', stop_price, ctx.get('state', {})

        if ctx['high'] >= tp_price:
            return 'TP', tp_price, ctx.get('state', {})

        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})

        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', ctx['current_price'], ctx.get('state', {})

        return None, None, ctx.get('state', {})


class TrendFilteredZScore(BaseStrategy):
    def generate_daily_indicators(self, df_daily):
        w = self.params.get('window', 10)
        df = df_daily.copy()
        df['SMA'] = df['Close'].rolling(window=w).mean()
        df['Std'] = df['Close'].rolling(window=w).std()
        df['Trend_Filter'] = df['Close'].rolling(window=50).mean()
        return df[['SMA', 'Std', 'Trend_Filter']].dropna()

    def check_signal(self, ctx):
        sma, std, trend = ctx['sma'], ctx['std'], ctx['trend']
        if pd.isna(sma) or pd.isna(std) or pd.isna(trend):
            return 'HOLD'
        threshold = self.params.get('z_score_threshold', 2.0)
        cp = _entry_price_field(ctx)
        return 'BUY' if cp <= sma - std * threshold and cp > trend else 'HOLD'

    def check_exit(self, ctx):
        if not ctx.get('at_bar_close', True):
            return None, None, ctx.get('state', {})
        cp, ep = ctx['current_price'], ctx['entry_price']
        pc = (cp - ep) / ep
        if pc >= ctx['take_profit']:
            return 'TP', cp, ctx.get('state', {})
        if pc <= -ctx['stop_loss']:
            return 'SL', cp, ctx.get('state', {})
        if ctx['hours_held'] >= ctx['max_hours_to_hold']:
            return 'TIME', cp, ctx.get('state', {})
        return None, None, ctx.get('state', {})
