#!/bin/bash
# Folder cleanup — moves dead files to _archive/ (recoverable, not deleted).
# Run from your bot folder:  bash cleanup_folder.sh
# Then review, and if happy, you can delete _archive/ later.

echo "Creating _archive/ ..."
mkdir -p _archive

echo "Archiving dead backtest experiments..."
for f in \
  backtest.py \
  backtest_crt.py \
  backtest_diag_ema.py \
  backtest_diagnostic.py \
  backtest_entries.py \
  backtest_etf_calls.py \
  backtest_exit_compare.py \
  backtest_exits.py \
  backtest_pullback.py \
  backtest_swing.py \
  backtest_tp_sweep.py \
  backtest_trend_exits.py \
  backtest_options_OLD.py \
  crt.py \
  foundation.py \
  options_new.py \
  strategy.py \
  test_bot.py \
  test_signals.py \
  requirements.txt.bak
do
  if [ -f "$f" ]; then
    mv "$f" _archive/ && echo "  archived: $f"
  fi
done

echo ""
echo "DONE. Live bot files + good backtests remain."
echo "Archived files are in _archive/ (recoverable)."
echo ""
echo "Remaining files:"
ls -1 *.py
