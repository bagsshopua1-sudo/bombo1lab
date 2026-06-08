"""
backtest/run_backtest.py — Запуск бэктеста из командной строки

Использование:
  python -m backtest.run_backtest                    # авто-список токенов
  python -m backtest.run_backtest --symbols WIF PEPE BONK
  python -m backtest.run_backtest --min-gain 20 --days 90
  python -m backtest.run_backtest --report report.txt
"""
import asyncio
import argparse
import time
import os

from loguru import logger
from backtest.backtester import Backtester, BacktestSummary


async def main():
    parser = argparse.ArgumentParser(description="Crypto Manipulation Monitor — Бэктест")
    parser.add_argument("--symbols",   nargs="+", help="Список токенов (напр. WIF PEPE BONK)")
    parser.add_argument("--min-gain",  type=float, default=30.0, help="Мин. рост для пампа %% (default: 30)")
    parser.add_argument("--days",      type=int,   default=180,  help="Период истории в днях (default: 180)")
    parser.add_argument("--max-syms",  type=int,   default=50,   help="Макс. токенов (default: 50)")
    parser.add_argument("--threshold", type=float, default=50.0, help="Порог скора (default: 50)")
    parser.add_argument("--report",    type=str,   default="",   help="Путь для сохранения отчёта")
    args = parser.parse_args()

    logger.info("=" * 52)
    logger.info("  CRYPTO MANIPULATION MONITOR — БЭКТЕСТ")
    logger.info("=" * 52)
    logger.info(f"  Период:      {args.days} дней")
    logger.info(f"  Мин. памп:   {args.min_gain}%")
    logger.info(f"  Порог скора: {args.threshold}")
    logger.info(f"  Макс. токенов: {args.max_syms}")
    logger.info("=" * 52)

    bt      = Backtester()
    summary = await bt.run(
        symbols        = args.symbols,
        min_gain_pct   = args.min_gain,
        lookback_days  = args.days,
        max_symbols    = args.max_syms,
        alert_threshold= args.threshold,
    )

    report = _build_report(summary, args)

    print("\n" + report)

    if args.report:
        os.makedirs(os.path.dirname(args.report) if os.path.dirname(args.report) else ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Отчёт сохранён: {args.report}")


def _build_report(s: BacktestSummary, args) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("  ОТЧЁТ БЭКТЕСТА — CRYPTO MANIPULATION MONITOR")
    lines.append("=" * 60)
    lines.append(f"  Дата:           {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  Период:         {args.days} дней")
    lines.append(f"  Мин. памп:      {args.min_gain}%")
    lines.append(f"  Порог алерта:   {args.threshold}/100")
    lines.append("")

    lines.append("─" * 60)
    lines.append("  ИТОГОВЫЕ МЕТРИКИ")
    lines.append("─" * 60)
    lines.append(f"  Пампов найдено:         {s.total_pumps}")
    lines.append(f"  Обнаружено (score≥50):  {s.detected_50}  ({s.recall_50:.1f}%)")
    lines.append(f"  Обнаружено (score≥70):  {s.detected_70}")
    lines.append(f"  Среднее упреждение:     {s.avg_hours_before:.1f}ч до пампа")
    lines.append("")

    # Интерпретация
    lines.append("─" * 60)
    lines.append("  ИНТЕРПРЕТАЦИЯ")
    lines.append("─" * 60)
    if s.recall_50 >= 60:
        lines.append(f"  ✓ Система обнаружила {s.recall_50:.0f}% пампов заранее — хороший результат")
    elif s.recall_50 >= 40:
        lines.append(f"  ~ Система обнаружила {s.recall_50:.0f}% пампов — приемлемо, есть куда расти")
    else:
        lines.append(f"  ✗ Система обнаружила {s.recall_50:.0f}% пампов — нужна доработка скоринга")

    if s.avg_hours_before >= 24:
        lines.append(f"  ✓ Упреждение {s.avg_hours_before:.0f}ч — достаточно для принятия решения")
    elif s.avg_hours_before >= 6:
        lines.append(f"  ~ Упреждение {s.avg_hours_before:.0f}ч — минимально достаточно")
    else:
        lines.append(f"  ✗ Упреждение {s.avg_hours_before:.0f}ч — слишком мало")

    lines.append("")
    lines.append("  Важно: бэктест симулирует только CVD + объёмные сигналы.")
    lines.append("  Полный скор (ончейн + деривативы) даст лучший результат.")
    lines.append("")

    # Детальные результаты
    if s.results:
        lines.append("─" * 60)
        lines.append("  ДЕТАЛЬНЫЕ РЕЗУЛЬТАТЫ (топ по величине пампа)")
        lines.append("─" * 60)
        lines.append(f"  {'Токен':<14} {'Памп':>6} {'Score7д':>8} {'Score3д':>8} {'Score1д':>8} {'Упреж':>8} {'CVD':>6} {'Статус'}")
        lines.append("  " + "─" * 58)

        top = sorted(s.results, key=lambda r: r.pump_event.gain_pct, reverse=True)[:30]
        for r in top:
            status = "✓ НАЙДЕН" if r.detected_50 else "✗ пропущен"
            uprezh = f"{r.hours_before:.0f}ч" if r.hours_before > 0 else "—"
            lines.append(
                f"  {r.symbol:<14} "
                f"{r.pump_event.gain_pct:>5.0f}% "
                f"{r.max_score_7d:>8.1f} "
                f"{r.max_score_3d:>8.1f} "
                f"{r.max_score_1d:>8.1f} "
                f"{uprezh:>8} "
                f"{r.cvd_max:>6.1f} "
                f"{status}"
            )

    lines.append("")
    lines.append("─" * 60)
    lines.append("  ПРОПУЩЕННЫЕ ПАМПЫ (для анализа)")
    lines.append("─" * 60)
    missed = [r for r in s.results if not r.detected_50]
    missed.sort(key=lambda r: r.pump_event.gain_pct, reverse=True)
    for r in missed[:10]:
        lines.append(
            f"  {r.symbol:<14} +{r.pump_event.gain_pct:.0f}% | "
            f"max score = {r.max_score_7d} | CVD = {r.cvd_max}"
        )
    if not missed:
        lines.append("  Все пампы обнаружены!")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
