import pathlib, sys, unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from translation_checks import required_numbers, required_number_counts, missing_numbers, repeated_numbers, added_large_numbers, normalize_translation_quantities, changed_loan_repayment, changed_explicit_roles, source_role_constraints
from translation_checks import repeated_source_terms

class TranslationChecks(unittest.TestCase):
    def test_spelled_integer_amounts_match_without_changing_their_values(self):
        for source, translation in [
                ('七千的额度', 'A seven thousand limit.'),
                ('给了七千块', 'Gave seven thousand dollars.'),
                ('一百元', 'A one hundred yuan item.'),
                ('七千五百元', 'Seven thousand and five hundred yuan.'),
                ('100元', 'A hundred yuan.'),
                ('两千五百万', 'Twenty-five million.')]:
            with self.subTest(translation=translation):
                self.assertEqual(missing_numbers(source, translation), [])
                self.assertEqual(added_large_numbers(source, translation), [])
        self.assertEqual(missing_numbers('160元', 'One hundred sixty-one yuan.'), ['160'])
        self.assertEqual(missing_numbers('1元', 'One person was there.'), ['1'])

    def test_colloquial_tail_amount_counts_the_thousands(self):
        # 两万七 = 27,000。2026-10-20 现场实测该式被误判为 20,000。
        cases = [('两万七说成三万', ['27000', '30000']),
                 ('两万七', ['27000']),
                 ('两亿七', ['270000000']),
                 ('七万五', ['75000'])]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(required_numbers(source), expected)

    def test_colloquial_tail_amount_is_normalized_for_the_translator(self):
        self.assertEqual(normalize_translation_quantities('两万七说成三万'), '27000说成30000')

    def test_complete_composites_and_ranges_are_not_rewritten_as_tail_amounts(self):
        for source, expected in [('两万七千', ['27000']),
                                 ('七八万', ['70000', '80000']),
                                 ('三万人', ['30000'])]:
            with self.subTest(source=source):
                self.assertEqual(required_numbers(source), expected)
        self.assertEqual(normalize_translation_quantities('两万七千'), '27000')

    def test_tail_digit_before_a_unit_stays_a_complete_amount(self):
        # 两万七百与两万七十按完整复合数计算，千位短式在此不适用。
        for source, expected in [('两万七百', ['20700']), ('两万七十', ['20070'])]:
            with self.subTest(source=source):
                self.assertEqual(required_numbers(source), expected)

    def test_spelled_ranges_preserve_both_endpoints(self):
        for translation in ['One or two thousand yuan.', 'Between one and two thousand yuan.',
                            'One thousand to two thousand yuan.']:
            self.assertEqual(missing_numbers('花一两千', translation), [], translation)
            self.assertEqual(added_large_numbers('花一两千', translation), [], translation)
        self.assertEqual(missing_numbers('花一两千', 'One or three thousand yuan.'), ['2000'])
        self.assertEqual(added_large_numbers('花一两千', 'One or three thousand yuan.'), ['3000'])
        self.assertEqual(missing_numbers('1000至2000000', 'Between one thousand and two million.'), [])
        self.assertEqual(missing_numbers('100至2000', 'Between one hundred and two thousand.'), [])

    def test_compound_range_scales_do_not_certify_wrong_endpoints(self):
        translation = 'One or two hundred thousand yuan.'
        self.assertEqual(missing_numbers('100000至200000元', translation), [])
        self.assertEqual(missing_numbers('1000至200000元', translation), ['1000'])
        self.assertEqual(added_large_numbers('1000至200000元', translation), ['100000'])
        translation = 'Between one and two thousand five hundred yuan.'
        self.assertEqual(missing_numbers('1000至2500元', translation), [])
        self.assertEqual(added_large_numbers('1000至2500元', translation), [])

    def test_conjoined_complete_amounts_keep_each_occurrence(self):
        translation = 'Seven thousand and eight thousand yuan.'
        self.assertEqual(missing_numbers('七千和八千', translation), [])
        self.assertEqual(repeated_numbers('七千和八千', translation), [])
        self.assertEqual(repeated_numbers('七千', 'Seven thousand and seven thousand.'), ['7000'])
        self.assertEqual(missing_numbers('七千和八千和九千', 'Seven thousand and eight thousand and nine thousand.'), [])

    def test_shared_scale_does_not_collapse_distinct_range_endpoints(self):
        translation = 'Between one and one thousand yuan.'
        self.assertEqual(missing_numbers('1元至1000元', translation), [])
        self.assertEqual(missing_numbers('1000元，再给1000元', translation), ['1000'])
        translation = 'Between one hundred and one hundred thousand yuan.'
        self.assertEqual(missing_numbers('100元至100000元', translation), [])
        self.assertEqual(missing_numbers('100000元，再给100000元', translation), ['100000'])

    def test_spelled_amount_checks_still_reject_added_missing_and_repeated_values(self):
        self.assertEqual(added_large_numbers('几千块', 'Seventy thousand yuan.'), ['70000'])
        self.assertEqual(added_large_numbers('二零一几年', 'Two thousand eighteen.'), ['2018'])
        self.assertEqual(missing_numbers('七千元', 'Six thousand yuan.'), ['7000'])
        self.assertEqual(repeated_numbers('七千元', 'Seven thousand, then seven thousand.'), ['7000'])
        self.assertEqual(missing_numbers('七千，再给七千', 'Seven thousand.'), ['7000'])
        self.assertEqual(missing_numbers('100元', 'A and one hundred yuan.'), [])

    def test_broken_mixed_numeral_does_not_become_a_required_value(self):
        # 实测：复核把「一千」写成「一00」，00 不应成为待核对数字。
        self.assertNotIn('00', required_numbers('充值了500输光，一00输光'))
        self.assertEqual(required_numbers('充值了500输光，一00输光'), ['500'])
        self.assertEqual(required_numbers('0.5元'), ['0.5'])

    def test_spoken_decade_requires_the_decade_not_a_truncated_year(self):
        self.assertEqual(normalize_translation_quantities('二零一几年以后'), '2010年代以后')
        self.assertEqual(required_numbers('二零一几年以后'), ['2010'])
        self.assertEqual(missing_numbers('二零一几年以后', 'After 201.'), ['2010'])
        self.assertEqual(missing_numbers('二零一几年以后', 'After the 2010s.'), [])
        self.assertEqual(added_large_numbers('二零一几年以后', 'After the 2010s.'), [])

    def test_refined_arabic_decade_uses_the_same_check(self):
        self.assertEqual(normalize_translation_quantities('就201几年以后'), '就2010年代以后')
        self.assertEqual(required_numbers('就201几年以后'), ['2010'])
        self.assertEqual(missing_numbers('就201几年以后', 'After 201, someone.'), ['2010'])
        self.assertEqual(missing_numbers('就201几年以后', 'After the 2010s, someone.'), [])

    def test_four_digit_year_is_not_read_as_a_decade(self):
        self.assertEqual(normalize_translation_quantities('二零一四年'), '二零一四年')
        self.assertEqual(required_numbers('二零一四年'), ['2014'])

    def test_spelled_value_does_not_bypass_item_count_check(self):
        from translation_checks import values_changed_to_item_counts
        self.assertEqual(values_changed_to_item_counts('价值38000元', 'Thirty-eight thousand items.'), ['38000'])

    def test_excessive_pf_loop_is_rejected_without_rejecting_source_repetitions(self):
        source = 'PF。PF。PF。PF。'
        self.assertEqual(repeated_source_terms(source, 'PF, PF, PF, PF.'), [])
        issues = repeated_source_terms(source, 'PF, '*238)
        self.assertEqual(issues, [{'term': 'PF', 'source_count': 4, 'longest_run': 238}])
        self.assertEqual(repeated_source_terms('PF是项目名', 'PF is ready. PF works.'), [])
        self.assertEqual(repeated_source_terms('yes yes yes', 'Yes, yes, yes.'), [])
        self.assertEqual(repeated_source_terms('PF', 'P.F. P.F. P.F.')[0]['longest_run'], 3)
        self.assertEqual(repeated_source_terms('R', 'R R R')[0]['term'], 'R')
        self.assertEqual(repeated_source_terms('P.F. P.F. P.F.', 'PF PF PF'), [])
        self.assertEqual(repeated_source_terms('PF的型号有1、2、3。', 'PF 1, PF 2, PF 3.'), [])

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
        # 七万五 是口语短式（75,000），已由 normalize_tail_amounts 处理，故不在本列。
        for source in ['七八万五千','一一万五千','七千五千','一0百','2万5千','一万五亿','七万五千五']:
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
