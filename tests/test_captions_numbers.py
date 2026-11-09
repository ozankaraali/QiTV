"""Turkish captions' numbers (services.captions.numbers_tr)."""

import unittest

from services.captions.numbers_tr import written


class TurkishNumbersTest(unittest.TestCase):
    def check(self, cases):
        for said, shown in cases:
            with self.subTest(said=said):
                self.assertEqual(written(said), shown)

    def test_digits_from_eleven_up(self):
        self.check(
            [
                ("On dört kardeşimiz vefat etti.", "14 kardeşimiz vefat etti."),
                ("on dörtte buluşalım", "14'te buluşalım"),
                ("kırk yıl geçti", "40 yıl geçti"),
                ("Bin beş yüz on yeni çalışan", "1510 yeni çalışan"),
                ("yedi bin civarında ada", "7 bin civarında ada"),
                ("bir milyon kişi", "1 milyon kişi"),
                ("yedi binde", "7 binde"),
                ("iki yüz elli bin kişi", "250 bin kişi"),
                ("iki milyon beş yüz bin lira", "2,5 milyon lira"),
                ("on iki bin beş yüz", "12.500"),
                ("on milyonluk bütçe", "10 milyonluk bütçe"),
                ("iki binde", "2000'de"),
                ("otuzlu yaşlar", "30'lu yaşlar"),
            ]
        )

    def test_years(self):
        self.check(
            [
                ("demek ki iki bin yediden bu tarafa", "demek ki 2007'den bu tarafa"),
                ("savaş bin dokuz yüz on dörtte başladı", "savaş 1914'te başladı"),
                ("iki bin yirmi beş yılında", "2025 yılında"),
            ]
        )

    def test_words_stay_for_ten_and_under(self):
        self.check(
            [
                ("üç kişi geldi, bir tanesi gitti", "üç kişi geldi, bir tanesi gitti"),
                ("beşinci kattayız", "beşinci kattayız"),
                ("iki üç kişi", "iki üç kişi"),
                ("beş on gün", "beş on gün"),
            ]
        )

    def test_units_stay_words(self):
        self.check(
            [
                ("Bir dakika bekle.", "Bir dakika bekle."),
                ("on beş dakika sonra", "15 dakika sonra"),
                ("iki kilo elma", "iki kilo elma"),
            ]
        )

    def test_everyday_words_stay(self):
        self.check(
            [
                ("onu gördüm, ona söyledim", "onu gördüm, ona söyledim"),
                ("yüzü kızardı", "yüzü kızardı"),
                ("yüz yüze konuştuk", "yüz yüze konuştuk"),
                ("arabaya bin", "arabaya bin"),
                ("masanın altında", "masanın altında"),
                ("yemeği on yedi kişi yedi", "yemeği 17 kişi yedi"),
                ("binlerce insan", "binlerce insan"),
                ("Beşiktaş kazandı", "Beşiktaş kazandı"),
                ("yirmi altın", "20 altın"),
            ]
        )

    def test_percent_time_date_decimal(self):
        self.check(
            [
                ("yüzde beş zam geldi", "%5 zam geldi"),
                ("Yüzde yüz eminim", "%100 eminim"),
                ("yüzde on beşi", "%15'i"),
                ("Saat on beş otuzda toplantı var.", "Saat 15.30'da toplantı var."),
                ("saat ikide geldi", "saat 2'de geldi"),
                ("saat onda", "saat 10'da"),
                ("Yirmi üç Nisan'da bayram var", "23 Nisan'da bayram var"),
                ("bir Mayıs", "1 Mayıs"),
                ("beş Mayıs günü", "5 Mayıs günü"),
                ("üç virgül beş puan", "3,5 puan"),
                # the owner, 2026-11-07: yetmek, not 70; a longer number still is
                ("gücünüz yetmiş.", "gücünüz yetmiş."),
                ("ekonomi kaynağınız yetmiş, bu.", "ekonomi kaynağınız yetmiş, bu."),
                ("yetmiş beş yaşında", "75 yaşında"),
                # the owner, 2026-11-07: "86,5000000"
                ("Seksen altı virgül beş milyon oldu.", "86,5 milyon oldu."),
                ("seksen altı virgül beş milyona çıktı", "86,5 milyona çıktı"),
                ("yirmi virgül iki milyar dolar", "20,2 milyar dolar"),
            ]
        )

    def test_ordinals_and_ranges(self):
        self.check(
            [
                ("on birinci sınıf", "11. sınıf"),
                ("on dördüncü kez", "14. kez"),
                ("birinci sırada", "birinci sırada"),
                ("on beş yirmi gün önce", "15-20 gün önce"),
                ("on beş onu gördü", "15 onu gördü"),
            ]
        )

    def test_a_clock_time_after_a_day_is_no_range(self):
        # the owner's TV promo, 2026-11-09: "21-30'da" was written for a time
        self.check(
            [
                ("yeni bölüm pazartesi yirmi bir otuzda", "yeni bölüm pazartesi 21.30'da"),
                ("bu akşam yirmi iki kırk beşte", "bu akşam 22.45'te"),
                ("pazar on bir on beş.", "pazar 11.15."),
                ("bugün iki üç kişi geldi", "bugün iki üç kişi geldi"),
                ("bugün on beş yirmi kişi geldi", "bugün 15-20 kişi geldi"),
            ]
        )

    def test_punctuation_and_spacing(self):
        self.check(
            [
                ("Kaç yaşındasın? On dokuz.", "Kaç yaşındasın? 19."),
                ("“on iki” dedi", "“12” dedi"),
                ("on, beş", "on, beş"),
                ("", ""),
            ]
        )


if __name__ == "__main__":
    unittest.main()
