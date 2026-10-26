"""The scale of EasyData datasets whose API unit is a bare currency (`usd`, `pkr`).

EasyData's metadata names the currency but not the scale, so ~2,800 series were stored as `usd` /
`pkr` although SBP publishes most of them in millions (a $6.2 bn bond stock read as "$6,156"). Each
entry is settled against SBP's own published table — the file, the line that states the scale, and
a value that matches ours — never inferred from magnitudes. `python -m scripts.apply_easydata_units`
writes them to `series.unit` (dry run first; `--commit` backs up the old units) and rescales the
spliced history listed in `easydata_splices.csv` (see SPLICED).
"""
from __future__ import annotations

import csv
import os

_HB = "https://www.sbp.org.pk/assets/document/"  # Handbook of Statistics on Pakistan Economy 2020 + ecodata files
_MSB = "https://www.sbp.org.pk/assets/documents/bulletin/MSB_OCT_2026_"  # Statistical Bulletin, October 2026
_AR25 = ("https://www.sbp.org.pk/assets/document/publications/"
         "reports-annual-aarFY25-Statistical-Supplement-Complete-Statistical-Supplement.pdf")
_AR24 = ("https://www.sbp.org.pk/assets/document/publications/reports/annual/aarFY24/Statistical-Supplement/"
         "Chapter-6%20(60-81)-Banking%20Sector.pdf")
_D = "2026-10-26"

# dataset code -> (unit, where SBP states the scale + the value that matches ours, date checked)
VERIFIED: dict[str, tuple[str, str, str]] = {
    "TS_GP_ED_EDSEC_Q": (
        "usd_mn",
        "https://www.sbp.org.pk/assets/document/sdds.pdf (Gross External Debt Position by Sector, "
        "headed '(Million US$)'): general government bonds and notes 6,156 at end-June 2026 = "
        "debt.external.bonds_notes_long_term_govt",
        "2026-10-26",
    ),
    # --- external sector ----------------------------------------------------------------------
    "TS_GP_BOP_SCRA_D": ("usd_th", _HB + "SCRA.pdf (Special Convertible Rupee Accounts, '(Thousand US $)'): "
                         "16-10-2026 equity inflow 712, outflow 1,145 = bop.inflows_scra_equity_overall 711.98, "
                         "bop.outflows_equity_overall_through_scra 1,144.82", _D),
    "TS_GP_BOP_SCRA_W": ("pkr_mn", _HB + "BS_SCRA.pdf (SCRA weekly position, '(Million PKR)'): 16/10/2026 closing "
                         "balance 38,460, closing market value of equity 535,698 = bop.closing_balance_scra 38,460.46, "
                         "bop.closing_market_value_equity 535,698.27", _D),
    "TS_GP_BOP_WR_M": ("usd_mn", _HB + "Homeremit_Arch.xlsx (Country-wise Workers' Remittances, '(Million US "
                       "Dollars)'): Jul-18 Saudi Arabia 437.48, U.A.E. 446.25 = remittances.workers_remittances_"
                       "received_saudi_arabia 437.48, remittances.workers_remittances_received_u_e 446.25", _D),
    "TS_GP_ES_SWR_M": ("usd_mn", _HB + "Homeremit_Arch.xlsx ('(Million US Dollars)'): total Sep-26 3,587.764715, "
                       "Jul-18 1,974.35 = external.workers_remittances (the seasonally adjusted twin shares its unit)", _D),
    "TS_GP_BOP_XMGS_M": ("usd_mn", _HB + "exp_import_BOP.pdf (Exports, Imports and Balance of Trade as per BOP, "
                         "'(Million US $)'): Sep-26 P exports 3,015, imports 6,172 = bop.goods_export_fob 3,015, "
                         "bop.goods_import_fob 6,172", _D),
    "TS_GP_BOP_IIPBPM5_Q": ("usd_mn", _HB + "Invest-BPM5.pdf (International Investment Position (BPM5), '(Stocks "
                            "in Million US Dollars)'): 30-06-2023 net IIP (125,942.7), liabilities 148,445.0 = "
                            "bop.international_investment_position_net -125,942.70, bop.liabilities 148,445.00", _D),
    "TS_GP_BOP_BPM5SUM_Q": ("usd_mn", "https://archive.sbp.org.pk/ecodata/Balancepayment_BPM5-Arch.xls (Summary "
                            "Balance of Payments as per BPM5, '(Million US$)'): FY03 Q1-Q4 current account 1,418/893/"
                            "1,196/563 = bop.current_account_balance_2; Q1 exports 2,643 = bop.exports_goods_fob_2", _D),
    "TS_GP_BOP_BPM4SUM_Q": ("usd_mn", "https://archive.sbp.org.pk/ecodata/Balancepayment_BPM4-Arch.xls (Summary "
                            "Balance of Payments MOF Format BPM4, 'Million US $'): FY02 Q4 exports 2,482, imports "
                            "2,573 = bop.exports_goods_fob, bop.imports_goods_fob at 2002-06-30", _D),
    "TS_GP_ES_BPMIN4964_Y": ("usd_mn", _HB + "Ch-7.xlsx (7.3 Invisibles by Category of Transactions, '(Million "
                             "Dollars)'): FY64 investment income (net) -59, FY63 payments 24 = external.investment_"
                             "income_net_debit -59, external.investment_income_net_payments_debit 24", _D),
    "TS_GP_ES_BPMIN6520_Y": ("usd_mn", _HB + "Ch-7.xlsx (7.3, '(Million Dollars)'): FY65 investment income (net) "
                             "-40, payments 51 = external.investmentincome_net -40, external.investmentincome_net_"
                             "paymentsdebit 51 (1,148 of 1,214 June values match year for year)", _D),
    "TS_GP_EXT_PAKRES_M": ("usd_mn", _HB + "forex.pdf (Liquid Foreign Exchange Reserves, '(MILLION US$)'): Sep 26 P "
                           "net reserves with SBP 21,510.5, with banks 4,826.0 = reserves.net_reserves_with_sbp "
                           "21,510.5, reserves.net_reserves_with_banks 4,826.02", _D),
    "TS_GP_BOP_FCD_M": ("usd_mn", _HB + "Chap-7.07b_.pdf (Handbook 7.7b Foreign Currency Deposits (FE-25) and "
                        "Utilization, '(Million Dollars)'): Jun-2003 deposits 2,296.4, resident 1,953.5 = bop.total_"
                        "foreign_currency_deposits_fe_25 2,296.4, bop.total_foreign_currency_deposits_fe_25_reside "
                        "1,953.5; " + _MSB + "Chap4.xlsx 4.12 ('Million US Dollars') Sep-26 FE-25 6,966.01", _D),
    "TS_GP_BOP_ONFCD_M": ("usd_mn", _HB + "Ch-7.xlsx (7.7 Balances Under Foreign Currency Account, '(Million "
                          "Dollars)'): FY84 old FCA 638.3, FY90 2,116 = bop.old_fca 638.27, 2,115.95", _D),
    "TS_GP_BOP_FCDBABRD_M": ("usd_mn", "https://archive.sbp.org.pk/ecodata/FX-Liabilities.pdf (Bank's Foreign "
                             "Currency Deposits and Borrowings from Abroad, 'Million USD'): Jun-22 FE-25 7,111, FE-03 "
                             "2,266, total 10,492 = bop.foreign_currency_deposits_fe25 7,110.51, bop.foreign_currency_"
                             "borrowings_abroad_fe_3 2,265.60, bop.fx_liabilities_ads_deposits_borrowings_depos 10,492.35", _D),
    "TS_GP_ES_KSORDA_M": ("usd_mn", "https://www.sbp.org.pk/our-operations/roshan-digital-accounts/key-statistics "
                          "('Amount in USD million'): Sep-26 funds received 14,157, utilized locally 8,937, NPC "
                          "(conventional) 740 = roshan_digital.total_funds_received_abroad_into_rdas_since 14,157.11, "
                          "roshan_digital.total_rda_funds_utilized_locally_since_incep 8,937.49", _D),
    # --- foreign investment -------------------------------------------------------------------
    "TS_GP_FI_FIPK_M": ("usd_mn", _HB + "Netinflow.xls (sheet Sector, '(Million US $)'): Power inflow Sep-26 "
                        "57.5721, Hydel 30.171085 = investment.fdi_inflows_power_sector 57.5721, "
                        "investment.fdi_inflows_hydel_sector 30.1711", _D),
    "TS_GP_FI_SUMFIPK_M": ("usd_mn", _HB + "NetinflowSummary.pdf (Summary of Foreign Investment in Pakistan, "
                           "'Million US$'): September 2026 total 3,083.8, foreign private investment 192.6 = "
                           "investment.net_total_foreign_investment 3,083.83, investment.net_foreign_private_"
                           "investment 192.60", _D),
    "TS_GP_FI_SUMFIPK49_Y": ("usd_mn", _HB + "FIS-FDI-Arch.xls (sheet 'FI Summary(Archiv) FY23 to date', 'Million "
                             "US$'): FY23 direct investment 1,626.986752, inflow 2,567.978418 = investment.net_"
                             "foreign_direct_investment_3, investment.total_fdi_inflows_2 at 2023-06-30", _D),
    "TS_GP_FI_REPATSEC_M": ("usd_mn", _HB + "Repatriation_PD.pdf (Repatriation of Profit/Dividend on Foreign "
                            "Investment-By Sector, '(Million US $)'): Sep-26 Power FDI 104.4, Coal 51.7 = investment."
                            "repatriation_profit_dividend_fdi_power_secto 104.35, ..._fdi_coal_sector 51.69", _D),
    "TS_GP_FI_REPATISC4_M": ("usd_mn", _HB + "RPDFI-ISIC.pdf ('(Million US$)', September 2026 (P)): financial and "
                             "insurance FDI 104.7, FI 114.4 = investment.repatriation_profit_dividend_fdi_financial_i "
                             "104.72, investment.repatriation_profit_dividend_fi_financial_in 114.44", _D),
    "TS_GP_FI_PORINV_HY": ("pkr_mn", _HB + "CPIS-Sdds-Assets-Data.pdf (Pakistan-Core Set of Portfolio Investment "
                           "Assets Data, End-December 2025, '(Million Rupees)'): grand total 117,044.3, Saudi Arabia "
                           "52,077.3 = investment.total_portfolio_investment_abroad_equity_deb_21 117,044.3, "
                           "investment.total_portfolio_investment_abroad_equity_deb_14 52,077.3", _D),
    # --- money and banking --------------------------------------------------------------------
    "TS_GP_BAM_ADVDEP_HY": ("pkr_mn", _AR24 + " (SBP Annual Report Statistical Supplement 2023-24, 6.7 Province/"
                            "Region wise Deposits by Categories and 6.8 Advances by Borrowers, '(Billion Rupees)'): "
                            "Jun-23 deposits 24,683.42 bn, government advances 1,887.26 bn = banking.total_"
                            "outstanding_deposits 24,683,416.30, banking.outstanding_advances_government "
                            "1,887,264.31 — EasyData holds these in millions", _D),
    "TS_GP_BAM_DISTUTI_HY": ("pkr_bn", _HB + "Pro-Reg-Advances.xls (Table-I Province/Region-wise Disbursement and "
                             "Utilization, '(Billion Rupees)'): Punjab disbursed Jan-Jun 2023P 9,088.11, Sindh "
                             "11,932.42 = banking.total_amount_disbursed_punjab 9,088.11, ..._sindh 11,932.42", _D),
    "TS_GP_BAM_DDCDH_M": ("pkr_mn", _HB + "DDholders.pdf (Deposits by category of deposit holders, '(Million "
                          "Rupees)', end Sep 2026 P): total 39,939,386, residents 38,248,773 = banking.total "
                          "39,939,385.89, banking.residents 38,248,772.98", _D),
    "TS_GP_BAM_ADVBYBOR_M": ("pkr_bn", _HB + "AdvaClasi-SecBor.pdf (Advances Classified by Securities/Borrowers, "
                             "'Outstanding Amount in Billion Rs.', Sep-2026P): grand total 15,145.9, merchandise "
                             "2,574.7 = banking.advances_banks_grand_total 15,145.89, banking.advances_banks_"
                             "against_merchandise_private_s 2,574.73", _D),
    "TS_GP_BAM_CRLONBOR_M": ("pkr_mn", _HB + "CreditLoans.pdf (Credit/Loans Classified by Borrowers, '(Million "
                             "Rupees)', Sep-26P): SBP credit to government (net) 411,991, scheduled banks' credit "
                             "to non-government sector 14,429,150 = banking.sbp_credit_government_sector "
                             "411,990.52, banking.scheduled_banks_credit_non_government_sector 14,429,150.37", _D),
    "TS_GP_BAM_FCS_Q": ("pkr_mn", _HB + "FCS.pdf (Financial Corporations Survey, 'Amount (Million Rs)'): domestic "
                        "claims 59,055,213, currency outside FCs 11,907,840 = banking.financial_corporations_fcs_"
                        "claims_domestic_s 59,055,212.72, banking.currency_outside_financial_corporations_fcs "
                        "11,907,840.21 (2026-06-30)", _D),
    "TS_GP_BAM_CENGOVTD_M": ("pkr_bn", _HB + "cengovdebt.pdf (Central Government Debt, '(In Billion Rupees)'): "
                             "Aug-26 domestic debt 58,757 = banking.central_government_domestic_debt 58,756.79; "
                             "CenGovDebt-Archive.xls (CGD Jan2011-Jun2022, '(In Billion Rupees)') Jun-22 long term "
                             "24,188.408298 = banking.central_govt_domestic_debt_long_term 24,188.41", _D),
    "TS_GP_BAM_GOVTDDL_M": ("pkr_bn", _HB + "D-Debt-Liabilities.pdf (Gross Government Domestic Debt and "
                            "Liabilities, '(In Billion Rupees)'): Aug-26 permanent debt 44,730, floating 10,562 = "
                            "banking.government_domestic_debt_liabilities_permane 44,730.19, banking.government_"
                            "domestic_debt_liabilities_floatin 10,562.39", _D),
    "TS_GP_BAM_OUTDDPSE_Q": ("pkr_bn", _HB + "PSEs.pdf (Outstanding Domestic Debt of PSEs, '(In Billion "
                             "Rupees)'): Jun-26 debt and liabilities 3,115, PSEs debt 2,238 = banking.public_sector_"
                             "enterprises_pses_debt_liabilit 3,115, banking.public_sector_enterprises_pses_debt 2,238", _D),
    "TS_GP_BAM_SMBYNSS_M": ("pkr_mn", _HB + "savings.pdf (Savings Mobilized by National Savings Schemes (Net), "
                            "'(Million Rs.)'): Jul-26 total 53,233, RIC 11,838 = banking.total_2 53,232.77, "
                            "banking.regular_income_certificate 11,838.05", _D),
    "TS_GP_PDL_NSSOA_M": ("pkr_mn", _MSB + "Chap5.xlsx (5.9 National Savings Schemes-Outstanding Amount, 'Million "
                          "Rupees'): Jun-26 certificates 2,401,854.57, postal life insurance 47,230.33 = debt.savings_"
                          "schemes_certificates_registered 2,401,854.90, debt.savings_schemes_postal_life_insurance_"
                          "scheme 47,230.33", _D),
    "TS_GP_PKDP_GOVTMS_M": ("pkr_bn", _HB + "BankNon-BankHoldingGoP.pdf (Investor-Wise Holdings of Government "
                            "Domestic Securities, '(PKR in billion)'): Jun-26 total 54,338, PIBs held by banks 27,118 "
                            "= debt.securities_holding_bank_non_bank 54,338.44, debt.pib_holding_banks 27,118.00", _D),
    # --- public finance: current (billions) ---------------------------------------------------
    "TS_GP_PF_FGRR21_Y": ("pkr_bn", _MSB + "Chap10.xlsx (10.2 Federal Government Revenue Receipts, '(Billion "
                          "Rupees)'): FY25 FBR direct taxes 5,791.667, FBR taxes 11,744.288 = fiscal.fbr_direct_"
                          "taxes_federal_govt_2, fiscal.fbr_taxes_federal_govt", _D),
    "TS_GP_PF_FGE21_Y": ("pkr_bn", _MSB + "Chap10.xlsx (10.3 Federal Government Expenditure and Lending, '(Billion "
                         "Rupees)'): FY25 current expenditures 15,814.571, overall balance -7,089.471 = fiscal.federal_"
                         "govt_current_expenditures 15,814.571, fiscal.federal_govt_budget_financing 7,089.471", _D),
    "TS_GP_PF_FGEXPENS_Y": ("pkr_bn", _MSB + "Chap10.xlsx (10.3, '(Billion Rupees)'): FY21 domestic interest "
                            "payments 2,523.8 = fiscal.federal_govt_domestic_interest_payments_debt_2 2,523.8", _D),
    "TS_GP_PF_FGRR_Y": ("pkr_bn", _MSB + "Chap10.xlsx (10.4 Federal Board of Revenue Tax Collection, '(Billion "
                        "Rupees)'): 2020-21 direct taxes 1,731.9, indirect 3,032.4 = fiscal.fbr_directtaxes_federal_"
                        "govt 1,731.9, fiscal.fbr_indirect_taxes_federal_govt_3 3,032.4", _D),
    "TS_GP_PF_FBRTAX_HY": ("pkr_bn", "https://www.sbp.org.pk/assets/document/Public_Finance16_statistical-bulletin-"
                           "october-2022.pdf (10.4 Federal Board of Revenue Tax Collection, '( Billion Rupees)'): "
                           "Dec-2021 direct taxes 259.0, total 600.5 = fiscal.fbr_direct_tax 259.0, fiscal.total_"
                           "fbr_tax 600.5", _D),
    "TS_GP_PF_PRES_Y": ("pkr_bn", _AR25 + " (SBP Annual Report Statistical Supplement 2024-25, 4.5 Provincial "
                        "Government Fiscal Operations, '(Billion Rupees)'): Balochistan FY24 sales tax on services "
                        "21.5, financing (112.3) = fiscal.baluchistan_governments_revenue_sales_tax_se 21.516, "
                        "fiscal.budget_financing_govt_baluchistan -112.262", _D),
    "TS_GP_PF_CPRE_Y": ("pkr_bn", _AR25 + " (4.4 Consolidated Provincial Governments Fiscal Operation, '(Billion "
                        "Rupees)'): FY24 bank financing (547.5), non-bank 29.2 = fiscal.budget_domestic_financing_"
                        "through_banks_cons -547.462, fiscal.budget_domestic_financing_through_non_banks 29.228", _D),
    "TS_GP_PF_ZAKATRCT_Y": ("pkr_mn", _AR25 + " (4.7 Zakat Receipts, '(Millions Rupees)'): FY25 total receipts "
                            "11,771, deducted at source 11,694 = fiscal.total_zakat_receipts 11,770.56, fiscal.total_"
                            "zakat_deducted_source 11,693.86", _D),
    "TS_GP_PF_FFGE_Y": ("pkr_mn", _HB + "Ch-3.xlsx (Handbook 3.3 Financing of Federal Government Expenditures, "
                        "'(Million Rupees)'): FY16 FBR direct taxes 1,324,000, FY20 gross federal resources 8,954,261 "
                        "= fiscal.fbr_direct_taxes_federal, fiscal.gross_federal_resources", _D),
    # --- public finance: history (millions; Handbook FY79-FY10 blocks) ------------------------
    "TS_GP_PF_FGRR1114_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.1 Federal Government Revenue Receipts, FY11-FY14 block "
                            "'(Billion Rupees)'): passport fee 9.9/10.9/16.2/19, gas development surcharge 30.4/23/"
                            "32.2/38.5 = fiscal.federal_revenue_citizenship_naturalization_p 9,900-19,000, fiscal."
                            "federal_revenue_development_surcharge_gas 30,400-38,500 — EasyData holds millions", _D),
    "TS_GP_PF_FGRR0110_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.1, '(Million Rupees)'): FY10 federal revenue 2,051,945, "
                            "FY98 custom duties to provinces 26,466 = fiscal.federal_govt_revenue, fiscal.federal_"
                            "custom_duties_transfer_provinces", _D),
    "TS_GP_PF_FGRR9000_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.1, '(Million Rupees)'): FY99 total taxes and surcharges "
                            "386,847, FY92 excise duty on sugar 3,450 = fiscal.federal_govt_total_tax_surcharge_"
                            "revenue, fiscal.federal_excise_duty_sugar_transfer_provinces", _D),
    "TS_GP_PF_FGRR8589_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.1, '(Million Rupees)'): FY89 total taxes 91,020, FY88 "
                            "75,983 = fiscal.federal_govt_total_revenue_2", _D),
    "TS_GP_PF_FGRR7984_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.1, '(Million Rupees)'): corporation tax FY79 2,171, FY80 "
                            "3,421 = fiscal.federal_revenue_corporate_tax", _D),
    "TS_GP_PF_FGE0510_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.2/3.3, '(Million Rupees)'): FY07 total expenditure "
                           "1,364,482, FY79 capital disbursements 19,293 = fiscal.total_expenditure, fiscal.capital_"
                           "disbursements", _D),
    "TS_GP_PF_FGE9399_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.2/3.3, '(Million Rupees)'): FY99 total expenditure "
                           "686,009, FY93 debt servicing 8,917.8 = fiscal.total_expenditures_3, fiscal.current_"
                           "capital_disbursements_debt_servicing_2", _D),
    "TS_GP_PF_FGE8992_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.2/3.3, '(Million Rupees)'): FY91 total expenditure "
                           "260,138, FY90 222,419 = fiscal.total_expenditures_2", _D),
    "TS_GP_PF_FGE7988_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.2 Federal Government Expenditures, '(Million Rupees)'): "
                           "FY79 current expenditure 25,233 = fiscal.federal_govt_current_expenditures 1979 (merged "
                           "from this dataset; it has no active series of its own)", _D),
    "TS_GP_PF_FFGE0015_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.3, '(Million Rupees)'): FY15 total receipts 3,768,714, "
                            "FY00 credit from banking system 39,963 = fiscal.federal_total_receipts, fiscal.federal_"
                            "credit_banking_system", _D),
    "TS_GP_PF_FFGE9499_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.3, '(Million Rupees)'): FY99 plan resources 127,040, "
                            "FY94 credit from banking system 12,508 = fiscal.external_resources_plan_resources, "
                            "fiscal.credit_banking_system", _D),
    "TS_GP_PF_FFGE9193_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.3, '(Million Rupees)'): FY91 permanent debt (net) "
                            "80,506, floating debt (net) -59,947 = fiscal.permanent_debt_net_3, fiscal.floating_"
                            "debt_net_3", _D),
    "TS_GP_PF_FFGE8590_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.3, '(Million Rupees)'): FY86 permanent debt (net) "
                            "20,307, FY85 advances non-interest bearing -355 = fiscal.permanent_debt_net_2, "
                            "fiscal.advances_non_interest_bearing_2", _D),
    "TS_GP_PF_FFGE7984_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.3, '(Million Rupees)'): FY83 external resources "
                            "14,342, FY79 commodity aid 2,105 = fiscal.plan_resources, fiscal.commodity_aid", _D),
    "TS_GP_PF_CPRE8810_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.4 Consolidated Provincial Receipts and Expenditures, "
                            "'(Million Rupees)'): FY10 current revenue expenditures 704,881.892, FY88 45,773 = "
                            "fiscal.consolidated_provincial_current_revenue_expe", _D),
    "TS_GP_PF_CPRE7987_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.4, '(Million Rupees)'): FY87 total revenue expenditures "
                            "44,579, FY79 current expenditure 8,793 = fiscal.total_revenue_expenditures, fiscal."
                            "current_expenditure", _D),
    "TS_GP_PF_PRES8810_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.5 Receipts and Expenditure by province, '(Million "
                            "Rupees)'): Punjab FY10 total revenue receipts 436,459, Balochistan FY88 current revenue "
                            "expenditures 3,777 = fiscal.total_revenue_receipts_punjab, fiscal.current_revenue_"
                            "expenditures_baluchistan", _D),
    "TS_GP_PF_PRES8287_Y": ("pkr_mn", _HB + "Ch-3.xlsx (3.5, '(Million Rupees)'): Punjab FY87 total revenue "
                            "expenditures 23,252, total receipts 21,253 = fiscal.total_revenue_expenditures_punjab, "
                            "fiscal.total_receipts_punjab", _D),
    # --- companies, national accounts, social -------------------------------------------------
    "TS_GP_FSA_NFCPSX05_Y": ("pkr_th", _HB + "FSA_NFS_2023.xlsx (Financial Statements Analysis of Non-Financial "
                             "Companies Listed on PSX 2018-2023, 'Amount in Rs. (000)'): 2023 non-current assets "
                             "7,667,914,544.126, operating fixed assets at cost 8,995,081,124.631 = corporate.non_"
                             "current_assets_non_financial_listed_comp, corporate.operating_fixed_assets_cost_non_"
                             "financial_li", _D),
    "TS_GP_FSA_NFCPSX04_Y": ("pkr_mn", _HB + "Ch-5.xlsx (Handbook 5.8 Balance Sheet Analysis of Non Financial "
                             "Companies (Listed at KSE), '(Million Rupees)' for 1966-2004): 1966 ordinary share "
                             "capital 2,050, 2004 debentures 23,474 = corporate.ordinary_share_capital_non_financial_"
                             "listed, corporate.debentures_bonds_certificates_non_financial", _D),
    "TS_GP_RLS_PAKGDP80_Y": ("pkr_mn", _HB + "Ch-1.xlsx (1.1 b Gross National Product at constant factor cost, "
                             "'(Million Rupees)'): FY99 GDP (FC) 625,233, agriculture 158,783 at 1980-81 prices = "
                             "gdp.gross_domestic_product_total_gross_value_add_3, gdp.agricultural_sector_gdp", _D),
    "TS_GP_RL_HLTHEXPNS_Y": ("pkr_mn", _HB + "Ch-11.xlsx (11.9 Expenditure on Health, 'Total Expenditure on Health "
                             "(Million Rupees)'): 1949-50 9.5, 2018-19 421,778 = social.total_expenditure_health", _D),
    "TS_GP_RL_DOCNEXPNS_Y": ("pkr_mn", _HB + "Ch-11.xlsx (11.7 Registered Medical and Paramedical Personnels and "
                             "Expenditure on Health, 'Expenditure (Million Rupees)'): 2019 development 58,624, "
                             "non-development 363,154 = social.total_development_expenditures, social.total_non_"
                             "development_expenditures", _D),
}

# Series inside a verified dataset that are per unit (per share, per person, per US$) — SBP prints them in
# rupees as they are, so they keep the bare currency.
PER_UNIT: dict[str, str] = {
    "corporate.basic_earnings_share_non_financial_listed_co": "FSA V3 'Basic earnings per share (Rs./share)'",
    "corporate.market_price_share_non_financial_listed_comp": "FSA V2 'Market price per share'",
    "corporate.paid_up_value_shares_non_financial_listed_co": "FSA V1 'Paid up value of shares (Rs.)'",
    "social.capita_health_expenditure": "Handbook 11.9 'Per Capita Health Expenditure (Rupees)'",
    "debt.external.us_last_day_weighted_avg_exchange_rate": "rupees per US$ (TS_GP_ED_PKEDLOUT_Q)",
    "money.cost_borrower_microfinance_banks": "operating expenses per borrower, rupees (TS_GP_MFS_FSIMFB_Q)",
}

# Datasets whose bare currency is right as it is: every series in them is a per-unit rate or price.
PER_UNIT_DATASETS: dict[str, str] = {
    "TS_GP_ER_FAERPKR_M": "average exchange rate, rupees per unit of foreign currency",
    "TS_GP_ER_FMEERPKR_M": "month-end exchange rate, rupees per unit of foreign currency",
    "TS_GP_ER_FAERUSD_M": "average exchange rate, US dollars per unit of foreign currency",
    "TS_GP_ER_FMEERUSD_M": "month-end exchange rate, US dollars per unit of foreign currency",
    "TS_GP_ES_FADERPKR_M": "daily average exchange rate, rupees per unit of foreign currency",
    "TS_GP_RL_CCSIND_M": "Consumer Confidence Survey: price in six months of an item that costs Rs 100 today",
}

# Bare datasets SBP's published tables could not settle (kept bare; listed in docs/backlog.md).
UNVERIFIED: dict[str, str] = {
    "TS_GP_RLS_SALEFMCG_Q": (
        "Sales of listed FMCG companies (1 series). Tried 2026-10-26: "
        "https://www.sbp.org.pk/assets/document/publications/FSAQ-Mar-26.xlsx (quarterly FSA, 'Amounts in 000', "
        "no FMCG aggregate), https://www.sbp.org.pk/assets/document/publications/MPIC_October_2026.pdf (no FMCG), "
        "https://www.sbp.org.pk/economic-data (no FMCG table), the EasyData dataset page (403 outside the portal)"
    ),
}

# The 2026-08-19 vintage merge copied history from older-vintage datasets into the current series,
# at the older dataset's scale (FY79-FY10 public finance in millions under FY11+ billions; FSA 1966-2004
# in millions under 2005+ thousands). (series' dataset, dataset the history came from) -> factor that
# brings those points to the series' unit. The points themselves are listed in easydata_splices.csv
# (matched to the merge backups obs_vintage_bak_20260819 / obs_vintage2_bak); EasyData no longer serves
# them (all are `missing_source` in dq-verify), so no job writes them again.
SPLICED: dict[tuple[str, str], float] = {
    ("TS_GP_FSA_NFCPSX05_Y", "TS_GP_FSA_NFCPSX04_Y"): 1000.0,
    ("TS_GP_PF_CPRE_Y", "TS_GP_PF_CPRE8810_Y"): 0.001,
    ("TS_GP_PF_FGE21_Y", "TS_GP_PF_FGE0510_Y"): 0.001,
    ("TS_GP_PF_FGE21_Y", "TS_GP_PF_FGE7988_Y"): 0.001,
    ("TS_GP_PF_FGE21_Y", "TS_GP_PF_FGE8992_Y"): 0.001,
    ("TS_GP_PF_FGE21_Y", "TS_GP_PF_FGE9399_Y"): 0.001,
    ("TS_GP_PF_FGEXPENS_Y", "TS_GP_PF_FGE0510_Y"): 0.001,
    ("TS_GP_PF_FGRR21_Y", "TS_GP_PF_FGRR1114_Y"): 0.001,
    ("TS_GP_PF_FGRR_Y", "TS_GP_PF_FGRR1114_Y"): 0.001,
    ("TS_GP_PF_FGRR_Y", "TS_GP_PF_FGRR9000_Y"): 0.001,
    ("TS_GP_PF_PRES_Y", "TS_GP_PF_PRES8287_Y"): 0.001,
}
SPLICES_PATH = os.path.join(os.path.dirname(__file__), "easydata_splices.csv")

# units that carry no scale — the only ones this registry may replace
BARE = ("usd", "pkr")
SCALE = {"usd_th": 1e3, "usd_mn": 1e6, "pkr_th": 1e3, "pkr_mn": 1e6, "pkr_bn": 1e9}


def scaled_unit(dataset_code: str | None, series_id: str, unit: str) -> str:
    """The verified unit for a series still on a bare currency, else `unit` unchanged."""
    if unit not in BARE or series_id in PER_UNIT or dataset_code not in VERIFIED:
        return unit
    return VERIFIED[dataset_code][0]


def load_splices() -> list[dict]:
    """easydata_splices.csv rows: series_id, obs_date, value (as held), dataset, merged_from."""
    with open(SPLICES_PATH, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
