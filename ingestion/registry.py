"""Central job registry.

Kept separate from `ingestion.run` (the CLI) so both the CLI runner and the
scheduler service import the same `JOBS` map without a circular dependency.
Add every new job here once it subclasses `IngestionJob`.
"""
from __future__ import annotations

from ingestion.framework import IngestionJob
from ingestion.jobs.cost_of_living import CostOfLivingJob
from ingestion.jobs.data_quality import DataQualityJob
from ingestion.jobs.derive_auctions import DeriveAuctionsJob
from ingestion.jobs.easydata_sync import EasyDataSyncJob
from ingestion.jobs.mufap_fund_navs import MufapFundNavsJob
from ingestion.jobs.mufap_fund_returns import MufapFundReturnsJob
from ingestion.jobs.mufap_debt_prices import MufapDebtPricesJob
from ingestion.jobs.mufap_fund_stats import MufapFundStatsJob
from ingestion.jobs.mufap_fund_portfolio import MufapFundPortfolioJob
from ingestion.jobs.mufap_debt_trades import MufapDebtTradesJob
from ingestion.jobs.mufap_tfc_valuations import MufapTfcValuationsJob
from ingestion.jobs.mufap_pkrv import MufapPkrvJob
from ingestion.jobs.pbs_cpi_groups import PbsCpiGroupsJob
from ingestion.jobs.pbs_cpi_monthly import PbsCpiMonthlyJob
from ingestion.jobs.pbs_external_trade import PbsExternalTradeJob
from ingestion.jobs.pbs_lsm import PbsLsmJob
from ingestion.jobs.pbs_spi_weekly import PbsSpiWeeklyJob
from ingestion.jobs.pta_telecom import PtaTelecomJob
from ingestion.jobs.sbp_auctions import SbpAuctionsJob
from ingestion.jobs.sbp_kibor import SbpKiborJob
from ingestion.jobs.sbp_payment_systems import SbpPaymentSystemsJob
from ingestion.jobs.sbp_policy_rate import SbpPolicyRateJob
from ingestion.jobs.sbp_sme_finance import SbpSmeFinanceJob

JOBS: dict[str, type[IngestionJob]] = {
    SbpKiborJob.name: SbpKiborJob,
    PbsSpiWeeklyJob.name: PbsSpiWeeklyJob,
    CostOfLivingJob.name: CostOfLivingJob,
    DeriveAuctionsJob.name: DeriveAuctionsJob,
    DataQualityJob.name: DataQualityJob,
    EasyDataSyncJob.name: EasyDataSyncJob,
    SbpPolicyRateJob.name: SbpPolicyRateJob,
    MufapPkrvJob.name: MufapPkrvJob,
    SbpAuctionsJob.name: SbpAuctionsJob,
    MufapFundNavsJob.name: MufapFundNavsJob,
    MufapFundReturnsJob.name: MufapFundReturnsJob,
    MufapFundStatsJob.name: MufapFundStatsJob,
    MufapFundPortfolioJob.name: MufapFundPortfolioJob,
    MufapDebtTradesJob.name: MufapDebtTradesJob,
    MufapDebtPricesJob.name: MufapDebtPricesJob,
    MufapTfcValuationsJob.name: MufapTfcValuationsJob,
    PbsExternalTradeJob.name: PbsExternalTradeJob,
    PbsCpiMonthlyJob.name: PbsCpiMonthlyJob,
    PbsLsmJob.name: PbsLsmJob,
    PbsCpiGroupsJob.name: PbsCpiGroupsJob,
    PtaTelecomJob.name: PtaTelecomJob,
    SbpPaymentSystemsJob.name: SbpPaymentSystemsJob,
    SbpSmeFinanceJob.name: SbpSmeFinanceJob,
}
