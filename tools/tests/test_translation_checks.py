import pathlib, sys, unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from translation_checks import required_numbers, required_number_counts, missing_numbers, repeated_numbers, added_large_numbers, normalize_translation_quantities, changed_loan_repayment, changed_explicit_roles, source_role_constraints

class TranslationChecks(unittest.TestCase):
    def test_repeated_units_do_not_create_an_invented_sum(self):
        self.assertEqual(required_numbers('七千五千又是大几千'), [])
        self.assertEqual(missing_numbers('七千五千又是大几千',
            'Seven thousand five thousand is a large number.'), [])
        self.assertEqual(required_numbers('七千五百和五千'), ['5000', '7500'])

    def test_spoken_year(self):
        self.assertEqual(required_numbers('一九八七年我考上大学'),['1987'])
        self.assertEqual(missing_numbers('一九八七年','It was 1987.'),[])
        self.assertEqual(missing_numbers('一九八七年','It was 1977.'),['1987'])
    def test_large_range(self):
        self.assertEqual(required_numbers('花了七八万'),['70000','80000'])
        self.assertEqual(missing_numbers('花了七八万','It cost 70,000–80,000.'),[])
        self.assertTrue(missing_numbers('花了七八万','over seven thousand'))
    def test_composed_quantities(self):
        self.assertEqual(required_numbers('十五万和两百亿'),['150000','20000000000'])
    def test_digits_and_decimals(self):
        self.assertEqual(missing_numbers('价格1.25，年份2024','1.25 in 2024'),[])
    def test_digit_unit_conversion(self):
        self.assertEqual(required_numbers('1.3万'), ['13000'])
        self.assertEqual(missing_numbers('1.3万', '13,000'), [])
    def test_does_not_impose_small_worded_numbers(self):
        self.assertEqual(required_numbers('有一个人参加二本院校的考试'),[])
    def test_calendar_month_names_preserve_the_number(self):
        self.assertEqual(missing_numbers('2022年5月', 'May 2022'), [])
        self.assertEqual(missing_numbers('到6月底', 'By late June'), [])
        self.assertEqual(missing_numbers('7月我上了16000', 'In July I paid 16,000'), [])
        self.assertEqual(missing_numbers('2022年5月', 'May 2023'), ['2022'])
    def test_month_name_does_not_mask_missing_quantity(self):
        self.assertEqual(missing_numbers('5月要付5元', 'In May I paid'), ['5'])
        self.assertEqual(missing_numbers('6月底', 'By late July'), ['6'])
        self.assertEqual(missing_numbers('5月2022年', 'I may leave in 2022'), ['5'])
    def test_count_units_allow_equivalent_words(self):
        self.assertEqual(missing_numbers('打5倍流水', 'Five times the turnover'), [])
        self.assertEqual(missing_numbers('5元', 'One person was there'), ['5'])
        self.assertEqual(missing_numbers('打5倍流水', 'Four times the turnover'), ['5'])
    def test_added_repeated_amount_is_rejected(self):
        self.assertEqual(repeated_numbers('要付11万块', 'Pay 110,000. Pay another 110,000.'), ['110000'])
        self.assertEqual(repeated_numbers('赢完100，赢完100后', 'After winning 100, after winning 100'), [])
        self.assertEqual(repeated_numbers('到6月底我赢到了差不', 'By late June I won 6.'), ['6'])
        self.assertEqual(repeated_numbers('到6月底我赢了6元', 'By late June I won 6 yuan.'), [])

    def test_new_large_amount_is_not_licensed_by_vague_source(self):
        self.assertEqual(added_large_numbers('几千块，700', 'Over 70,000, and 700.'), ['70000'])
        self.assertEqual(required_numbers('大几千、数百'), [])
        self.assertEqual(added_large_numbers('两千块', '2,000 yuan'), [])
        self.assertEqual(added_large_numbers('三百块', '300 yuan'), [])
        self.assertEqual(required_numbers('两三千'), ['2000','3000'])
        self.assertEqual(required_numbers('三百五十和一千零五'), ['350','1005'])
        self.assertEqual(required_numbers('两千五百万'), ['25000000'])
        self.assertEqual(required_numbers('2千'), ['2000'])

    def test_normalization_retains_equivalent_ranges_and_uncertainty(self):
        self.assertEqual(normalize_translation_quantities('应该是两三万，另有11万和1.3万。'), '应该是20000–30000，另有110000和13000。')
        self.assertEqual(normalize_translation_quantities('几万，一0百，5月'), '几万，一0百，5月')
        self.assertEqual(normalize_translation_quantities('一亿五千万，十万八千，2万5千'), '150000000，108000，2万5千')

    def test_exact_composites_are_one_amount_not_licensed_components(self):
        source='先拿了七千，又拿了五千，不是七万五千。'
        self.assertEqual(required_number_counts(source), {'7000':1,'5000':1,'75000':1})
        self.assertEqual(missing_numbers(source,'First 7,000, then 5,000, not 70,000.'), ['75000'])
        self.assertEqual(added_large_numbers(source,'First 7,000, then 5,000, not 70,000.'), ['70000'])
        self.assertEqual(missing_numbers(source,'First 7,000, then 5,000, not 75,000.'), [])
        self.assertEqual(normalize_translation_quantities('七万五千'), '75000')

    def test_composites_across_scales_and_zeros(self):
        for source, amount in [('一亿五千万','150000000'),('一亿零五万','100050000'),('十万八千','108000'),('七万零五','70005'),('两千五百万','25000000')]:
            self.assertEqual(required_numbers(source), [amount], source)
            self.assertEqual(normalize_translation_quantities(source), amount, source)

    def test_ambiguous_or_mixed_composites_are_not_normalized(self):
        for source in ['七八万五千','一一万五千','七千五千','一0百','2万5千','一万五亿','七万五','七万五千五']:
            self.assertEqual(normalize_translation_quantities(source), source)

    def test_missing_intentional_repeated_amount_is_rejected(self):
        self.assertEqual(missing_numbers('给了1000，又给了1000','Gave 1,000.'), ['1000'])
        self.assertEqual(missing_numbers('给了1000，又给了1000','Gave 1,000, then another 1,000.'), [])

    def test_measured_person_and_reflexive_errors(self):
        self.assertEqual(changed_explicit_roles('我说谎。他不管对面是谁。', "I lie. It doesn't matter who is there."), ['explicit_third_person_missing'])
        self.assertEqual(changed_explicit_roles('他不管对面是谁。', "He doesn't care who is there."), [])
        self.assertEqual(changed_explicit_roles('妈妈已经有点不相信了妈妈。', 'Mom doubts herself.'), ['unsupported_mother_reflexive'])
        self.assertEqual(changed_explicit_roles('妈妈不相信自己了。', 'Mom doubts herself.'), [])
        self.assertEqual(changed_explicit_roles('妈妈不相信了。', 'Mom is skeptical.'), [])
        self.assertEqual(source_role_constraints('我在想自己'), [])


    def test_explicit_repayment_is_not_borrowing(self):
        self.assertTrue(changed_loan_repayment('充值翻倍还贷款。', 'Top up and double, then take out a loan.'))
        self.assertTrue(changed_loan_repayment('还贷款', 'Pay money.'))
        self.assertFalse(changed_loan_repayment('充值翻倍还贷款。', 'Top up, double it and repay the loan.'))
        self.assertFalse(changed_loan_repayment('我借了700元还贷款', 'I borrowed 700 to repay the loan.'))
        self.assertFalse(changed_loan_repayment('我没有还贷款', 'I did not repay the loan.'))
        self.assertFalse(changed_loan_repayment('我借贷款购买商品', 'I took a loan to buy goods.'))

if __name__=='__main__':unittest.main()
