# WAT Reselling Agent - Quick Commands
# Usage: .\run.ps1 <command>
# Run from the project folder: cd "C:\Users\jorda\OneDrive\Desktop\reselling-agent"

param([string]$cmd = "help", [string]$category = "")

$py = "python"
$agent = "agents\scheduler.py"

switch ($cmd) {

    # -- Refresh prices & stock for the whole sheet ---------------------------
    "sweep" {
        Write-Host "Running recheck (CHECK FAILED + missing prices only)..." -ForegroundColor Cyan
        & $py $agent --mode recheck
    }

    "sweep-all" {
        Write-Host "Running FULL force recheck (all rows)..." -ForegroundColor Yellow
        & $py $agent --mode recheck --force
    }

    # -- Research: score PENDING rows and fill in tier/price data -------------
    "research" {
        if ($category) {
            Write-Host "Running research for category: $category" -ForegroundColor Cyan
            & $py $agent --mode research --category $category
        } else {
            Write-Host "Running research on all PENDING rows..." -ForegroundColor Cyan
            & $py $agent --mode research
        }
    }

    # -- Discovery: find new Costco products, add as PENDING ------------------
    "discover" {
        if ($category) {
            Write-Host "Running discovery for category: $category" -ForegroundColor Cyan
            & $py $agent --mode discovery --category $category
        } else {
            Write-Host "Running discovery (all categories)..." -ForegroundColor Cyan
            & $py $agent --mode discovery
        }
    }

    # -- Active monitor: check ACTIVE listings for stock/price changes --------
    "monitor" {
        Write-Host "Running active monitor..." -ForegroundColor Cyan
        & $py $agent --mode active
    }

    # -- Daily sweep: APPROVED -> READY, PAUSED_OOS recheck ------------------
    "daily" {
        Write-Host "Running daily sweep..." -ForegroundColor Cyan
        & $py $agent --mode daily
    }

    # -- Rotation: weekly digest, score all ACTIVE products ------------------
    "rotation" {
        Write-Host "Running rotation digest..." -ForegroundColor Cyan
        & $py $agent --mode rotation
    }

    # -- Refresh cookies: run when Costco scrapes start returning CHECK FAILED -
    "cookies" {
        Write-Host "Refreshing Costco session cookies..." -ForegroundColor Yellow
        Write-Host "A Chrome window will open - log into Costco, then run the export step." -ForegroundColor Gray
        & $py tools\setup_costco_session.py
    }

    # -- Sheet setup: reformat headers, column widths, conditional formatting -
    "setup-sheet" {
        Write-Host "Running sheet setup/formatter..." -ForegroundColor Cyan
        & $py agents\setup_sheet.py
    }

    # -- Interactive menu -----------------------------------------------------
    "menu" {
        & $py agents\menu.py
    }

    # -- Help -----------------------------------------------------------------
    default {
        Write-Host ""
        Write-Host "WAT Reselling Agent - Commands" -ForegroundColor White
        Write-Host "==============================" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "DAILY USE" -ForegroundColor Yellow
        Write-Host "  .\run.ps1 sweep           Recheck CHECK FAILED rows + missing prices"
        Write-Host "  .\run.ps1 sweep-all        Force-refresh every row (cost, stock, eBay prices)"
        Write-Host "  .\run.ps1 monitor          Check ACTIVE listings for stock/price changes"
        Write-Host ""
        Write-Host "RESEARCH" -ForegroundColor Yellow
        Write-Host "  .\run.ps1 research         Score all PENDING rows (fills tier, price, comps)"
        Write-Host "  .\run.ps1 research -category 'Jewelry'   Research one category only"
        Write-Host "  .\run.ps1 discover         Find new Costco products, add as PENDING"
        Write-Host "  .\run.ps1 discover -category 'Outdoor Furniture'"
        Write-Host ""
        Write-Host "MAINTENANCE" -ForegroundColor Yellow
        Write-Host "  .\run.ps1 daily            APPROVED->READY sweep, PAUSED_OOS recheck"
        Write-Host "  .\run.ps1 rotation         Weekly digest - score ACTIVE products"
        Write-Host "  .\run.ps1 cookies          Refresh Costco session (run when scrapes fail)"
        Write-Host "  .\run.ps1 setup-sheet      Reformat sheet headers and column widths"
        Write-Host "  .\run.ps1 menu             Launch interactive menu"
        Write-Host ""
        Write-Host "TIP: Run from the project folder:" -ForegroundColor DarkGray
        Write-Host "  cd 'C:\Users\jorda\OneDrive\Desktop\reselling-agent'" -ForegroundColor DarkGray
        Write-Host ""
    }
}
