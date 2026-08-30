/** Plain-language names for closed demo rule ids.  Unknown ids stay as-is. */
export const RULE_NAME_ZH: Record<string, string> = {
  R_VIN_CROSS: "车辆识别代号（VIN）是否一致",
  R_ENGINE_CROSS: "发动机号是否一致",
  R_NAME_FUZZY: "姓名是否一致",
  R_BRAND_CROSS: "品牌是否一致",
  R_MODEL_CROSS: "型号是否一致",
  R_ID_NUMBER: "证件号是否一致",
  R_PLATE_CROSS: "车牌号是否一致",
  R_AMOUNT_TOL: "融资金额是否在容差内",
  R_REG_DATE: "登记日期是否一致",
  R_REG_NO: "登记证编号是否一致",
  R_ADDR_FUZZY: "地址是否一致",
  R_ID_REQUIRED: "有融资金额时身份证是否齐全",
  R_PLATE_IN_POLICY: "车牌是否出现在保单号牌列表中",
};

export function ruleTitle(ruleId: string, fallback?: string | null): string {
  return RULE_NAME_ZH[ruleId] ?? fallback ?? ruleId;
}

export function humanHonestyNote(): string {
  return "以上指标来自固定评测集上的交叉核验，不是真实影像 OCR 的全量成绩。";
}

export function humanEvalWarning(raw: string): string {
  if (raw.startsWith("skipped_unlabeled_fixtures")) {
    return "有少量未标注样例未纳入本次统计。";
  }
  if (raw.startsWith("smoke_mode")) {
    return "当前评测集没有可用标注，无法计算误报和漏报。";
  }
  return "评测过程中有提示，不影响已展示的指标口径。";
}

const DOC_TYPE_ZH: Record<string, string> = {
  机动车登记证书: "机动车登记证书",
  交强险保单: "交强险保单",
  融资租赁合同: "融资租赁合同",
  发票: "发票",
  身份证: "身份证",
};

const FIELD_ZH: Record<string, string> = {
  vin: "车辆识别代号（VIN）",
  engine_no: "发动机号",
  owner_name: "所有人姓名",
  lessee_name: "承租人姓名",
  insured_name: "被保险人姓名",
  buyer_name: "购买人姓名",
  plate_no: "号牌号码",
  plate_list: "号牌列表",
  id_number: "证件号",
  financed_amount: "融资金额",
  invoice_amount: "发票金额",
  reg_cert_no: "登记证编号",
  reg_date: "登记日期",
  contract_date: "合同日期",
  address: "地址",
  brand: "品牌",
  model: "型号",
};

export function documentTypeLabel(docType: string): string {
  return DOC_TYPE_ZH[docType] ?? docType;
}

export function fieldLabel(field: string): string {
  return FIELD_ZH[field] ?? field;
}

export function verdictLabel(verdict: string): string {
  if (verdict === "consistent") return "一致";
  if (verdict === "inconsistent") return "不一致";
  if (verdict === "uncertain") return "存疑";
  if (verdict === "skipped") return "跳过";
  return verdict;
}
