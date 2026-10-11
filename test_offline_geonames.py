"""Standalone synthetic tests; real GeoNames data is verified in Cloud Shell."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from geonames_lookup import lookup_place, normalized_key
from build_geonames_db import build


def raw_record(gid, name, alias, lat, lon, fclass, fcode, country, admin, population):
    fields = [str(gid), name, name, alias, str(lat), str(lon), fclass, fcode, country,
              '', admin, '', '', '', str(population), '', '', 'Asia/Tokyo', '2026-10-11']
    return '\t'.join(fields)+'\n'


class OfflineGeoTests(unittest.TestCase):
    def test_lookup_without_external_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            admin = tmp/'admin.txt'
            admin.write_text('JP.13\tHyogo\tHyogo\t0\nJP.40\tTokyo\tTokyo\t0\n')
            countries = tmp/'countries.txt'
            countries.write_text('JP\tJPN\t392\tJA\tJapan\nFR\tFRA\t250\tFR\tFrance\nUS\tUSA\t840\tUS\tUnited States\n')
            city = tmp/'cities500.zip'
            japan = tmp/'JP.zip'
            with zipfile.ZipFile(city,'w') as z:
                z.writestr('cities500.txt', ''.join([
                    raw_record(1,'Tokyo','東京,Tokyo',35.68,139.69,'P','PPLC','JP','40',10000000),
                    raw_record(2,'Akashi','明石,明石市,Akashi',34.65,134.99,'P','PPL','JP','13',300000),
                    raw_record(3,'Paris','パリ,Paris',48.86,2.35,'P','PPLC','FR','A8',2000000),
                    raw_record(4,'Paris','Paris',33.66,-95.55,'P','PPL','US','TX',25000),
                ]))
            with zipfile.ZipFile(japan,'w') as z:
                z.writestr('JP.txt', ''.join([
                    raw_record(2,'Akashi','明石,明石市,Akashi',34.65,134.99,'P','PPL','JP','13',300000),
                    raw_record(5,'Chiyoda','千代田区,千代田',35.69,139.75,'A','ADM2','JP','40',0),
                ]))
            # Source has a 40,000-record floor to catch incomplete real downloads.
            # Unit fixtures only check parsing/lookup through the lightweight override.
            db = tmp/'sample.sqlite3'
            try:
                build(city,japan,admin,countries,db,minimum_places=1,minimum_jp=1)
            except TypeError:
                self.fail('Builder needs minimum_places/minimum_jp optional test parameters')
            for query, lat in [('東京',35.68),('東京都千代田区',35.69),('兵庫県明石市',34.65),
                               ('明石市',34.65),('Paris France',48.86),('Paris, France',48.86),
                               ('34.5, 135.25',34.5)]:
                with self.subTest(query=query):
                    result = lookup_place(query, db)
                    self.assertAlmostEqual(result['latitude'],lat)
            with self.assertRaises(ValueError):
                lookup_place('地名が存在しない', db)

    def test_japanese_diacritics(self):
        self.assertNotEqual(normalized_key('か'), normalized_key('が'))
        self.assertEqual(normalized_key('Hyōgo'), normalized_key('Hyogo'))

if __name__=='__main__':
    unittest.main()
