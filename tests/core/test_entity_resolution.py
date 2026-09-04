from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import threading
import zlib

import pytest

from auto_foundry_core import (
    CanonicalMapping,
    EntityResolutionResult,
    EntityResolutionWorkspace,
    IdentityDomainScope,
    IdentityDecision,
    LivingEnterpriseModel,
    OntologyItem,
    ResolutionCapacity,
    RunContext,
    replay_ready_commits,
    StaleIdentityScopeError,
)
from auto_foundry_core.durable import ItemWorkspace
from auto_foundry_core.entity_resolution import _iter_replay_batches
from auto_foundry_core.integration import IntegrationSession
from auto_foundry_core.lem_projection import LivingEnterpriseModelProjector
from auto_foundry_core.lifecycle import RunLifecycle
from auto_foundry_core.prepared import PreparedAssetRegistry


SOURCE_HASH = "a" * 64
_G5_LEGACY_STATE_SHA256 = (
    "aa5ba529127ca3e780dfafec909cdc27c917c41cb769cbd2626f9e3c68435671"
)
_G5_LEGACY_STATE_ZLIB_B64 = (
    "eNrtfVtz27iW7vv5FSw/zcyxbNxByrUf3I7ntM90Eo/jpGtqakoFAqCtjiRqSCqJ967+72cBvOtiR7bFTOV0V6dbIQEsXNblWwDW"
    "4j+OtFoqPS0ejsb/OFILNXsoplrNJunXhc2Oxvj4yC4KeD3JbJ7OVsU0XRyN2fFRvrR6qmbTvDga0+OjIi2gktLF9Is9God/Hh+Z"
    "dK6mi9w1q1d5kc5dc0BCa7ssrJks7cJMF3cTnc7nU2gkUbPcHkN3FunC92BqSspH46OLugF474tP5moxTWxeTO5Vfg8lsI4RY4gk"
    "UhsRURsKG0kaCcuT2AoWhZHATNqQcFeIYR5HUShFGCMaE4UJtGymuU6/2Az6Fj9MpoWdQxeg6ZvLfx8hX8CPqHyo2x6l8R9WF5Pi"
    "YWn7Xc2Umy01s00jwcJakwd15VFu7+YwyMBYGI8J3J/MFqtscZrZZAV/s1maQxtQo8gDpeFveWDdFNhM2+Pg8ub62NdKpgu10PYk"
    "uKxfBqvc5sHlxeji44fb0b8E5WwmU5vlvl75ftvLqq2g7ELuCga/XI8u1ss5usW9DXI7g/HbuuvQwS+uXN2FerDBQs1tfhYsUij4"
    "ZWq/Qo16iYO5Wi6BGQK1Ku7TbPp3Vy+dx9OFewhEctedJbAglPezmp+4CbZLNc2Ag1aLwrNqv0y9hsCD/3nUzNrY/ZrUvXKrCSye"
    "LcfuyWSRwt+qCRg3ZeKle/Ffrv1aBmr5qKRj5N8A88CPbLXAow5/rFea3IPQpBnw9X+WTa5mDRsnJCGMgIwwihImOGE4jnCUEEGV"
    "FVEcK6ot4zGXSIVxTCghRnBkFUtQGFHpybnJdaIG43ASlpeEFmnhGPFWZXe2XC19b/XnYKny3JpxswQ6XRSOJ1cLfa8Wd9acBTAu"
    "x4VQyZUtTl1nT/2sn+bpyjFbXJIK7DdQAWdBKeRBkmaBgmmY6qAU23LRDjJgkFoFTHBEEBEjFI4wu8XRGJMxRSdQixHxvxEaI9RW"
    "yEClJVBjOVOLRb1yIzz6gqQrBYM2U+2aLEdz9Gddc7Lx6nvbLGcLGGBReKac2S92NmGTZDWbTcw0AzVrVKFgjidfp8X9ZGmSfLJM"
    "Z1M9tfkpwpNMfXUKc6kWDxP7bZlmRX7acPZpn7OXWZpMZzY/0fkXoP06tLKl+9MSmau8sNlrkqikr9KAk5k1d88nABond01rqAbK"
    "wROo1NSk0m/+7dKLp7NmJ99m+Tcn6zkoEScwmVXmwS1+o7XTzMB/oQ8wvTOnv19s1967FncbNwOWChGpFKIgJ5wizERilNGUWPil"
    "QTrikNM4ZJwgTqVNGFjBJJRWShpy/ZRxwzuM25aR7jJ39Qg2bB4GRfPfqylIvdM46UJPZ1NfJkiydB6AoGjrbWBxn6Wru3tvm4yd"
    "AY7IHo6D399+CPL76dIVOQ5u4W9Fpha5W8nSAqWL2XRhR05XLexs3UqcBbmaL4GHgs/2oTRlMBPFdKELT+jD+xFYw4v3b0ee1Oj9"
    "zZtR2e5t9bf1Fo+DPA1UUMDyQKvNqgZ+qlqDNs29rYcisQU1aANvu0f5A1SbB3+kgIygSuCZbATTkgDPFoGeqel8f9MG1itPS9u1"
    "ZuZ8r8o3X+f5OLMz5Xi0fOxUFSC3jef72jpfK9/T0skQRSRWUaJ1LCLNDKWCJzimJBKKYWxjjpgJFRFGh8Rwjo1SktCEGcmJ1C+w"
    "dAnwVmBWS5hyGHVnDfVs5bRZHsAMesOnZrOgsnDVyoJeAcs4/e+VPQuWDsGVb7P062nDWF7Q1J31fARcderYdrr4Ami54kPQdU5H"
    "FRUayqELm6bx1WZoh2lkYx6dcBkJwX5a01iZqxwUUl5x9z3oc1jjQ1jFLhmnlQ5CpNaNBxlIH0ocbsK20YGGYe1fkwyoPPdnUhuQ"
    "Qwxlg0Y1jD9ysEyvRQR0tPvTEHnVEdSNg2HVnx1yeclCkPL3wwR038S5EtBoMTGpXvl+n7oRLD08UlkGfuQkn6lJvprPVfZwMjeP"
    "Ay9Q5Hb6RcUOXbwQcd20Te2EXZyHJkloFEbMMBppKRVALGFjnahQCxQhLBNuiVEkJthIYUANR5QxwgkOMXsKdoW7YFfW7dx2vNXr"
    "/wboCrugq/K1N2u2aKXaWTi/cWYqnYJBK2G3s2uAeOD5Uj14lFY9r4uBSofBAVyHUm6cZmWDr2nm2GjknI+tBZ1ZjAHYOHSk05nb"
    "PYDujyzMxGxLhRpB9eHjWWWYR7PUm+6erx6ou7vM3pU4M7N+IypYwhCnbtjxqqjhWGW92ymZq0LfO092Dg2CdS6AenC3sg457IvL"
    "+q/G5zdX5dh+83NY/eXdah5X2wRrpa/LKf/O0u/L2f+9nPx/hbl/sspFM/eXbuqr8jf1xH9Hldt7J6T7ocUec++DGG3IwfnRUmuZ"
    "mJBJri1h1BCKYm2piQTmgJASaoywSlJsIokpYhJFJlKY8mcgxsub0c3lxeXVp/NffruEn9c3lx8u391+GCGExyWvOP0DHJEHyjh+"
    "9hsgs4egSIMEmMsheuAzr0QDj7KdDwE6EDyTasMFBKLG//Bztajmq9wN9Pst7VbMJkh8tUnZAhIJHnMxRuIEMyk4/llBYr3hoLJJ"
    "pXxeuOnwBJFKlR5gZ6MiMqk0cT2cfFKp5IlTyX6DYzzO760t/lapi+DGt/D65r7ukXIqs1YdVR1n7vcmSCfeTEwKr3laAp3W+wUu"
    "355f/QbyysTk/GZ09e4T/IZ/8EnxrRiSvOyRJ0OTD3vk6dDkox55NjB5jnrk+dDkcY+8GJo86ZGXQ5OnPfLh0ORZj3w0NHneJY/R"
    "0OR7Wg8PrfV4T+vhobUe72k9PLTW4z2th4fWeqKn9XCp9bb51wATwFgD6h4p8we4PS860riqGztv29rpYTMpQ8sM4jaWCaIyTACx"
    "guccE6F1bHSorQEcGzIVWqVobOCfBEuJQhAlqp882BBrHvbWka672NtH0PWx2xe9Df/K5zYO/AMWWkCbwRz65gu2BPPALXMA6D9L"
    "Pc5vW2uA03Hg17Rylku81Jyr+13jckads7Klv81hrv0G/tSWAwW0j+O6hcD1m3/ddA+3lHurss8GPLvvKuxdykdKvq3msq0Bvup+"
    "zucOBtjHDQW/SdsEI6MtCBUTlBsrY5PEMQ0BWWHJjE4YZpwrzmNs45gARytsBAF/TNOn3dCrhbFO5NxClkXHAUEBzPlpPZ9dpmnE"
    "EHzKkms800QIbeM+t7GSV3siXW/T8WTqNkbShc7ACW540Z9YnFXsqmbunM7m9+nMBHdqWZKaq1mSZnNr6usAzZFq96ij6wo3RyTe"
    "X950a19tkne6taAPOUeE/7TXAuqN6YblJzU3HGhrtyXUctukZpuX+HxPEf6qMgu8m9sJCK3+XOq4SZ4un0UNuweTeJW7A5y8Q+b3"
    "Xyfnb/7vxw+3by/f3U5+/3UERZ0jCcUPToUMQoUOQoUNQoUPQkUMQkUOQiUchEo0BBXws4agMojs40FkHw8i+3gQ2ceDyD4eRPbx"
    "ILKPB5F9PIjsk2fKPtnH5j8Pxuxh7g9MgB6aADs0AX5oAuLQBOShCYSHJhAdmACY8QMTOLQk40NLMj60JONDSzI+tCTjQ0syPrQk"
    "40NLMj60JBP0Kqe9655+Z6Nh+x64xJOrD+g1T3z37wJpukB+VBdo0wX6o7rAmi6wH9UF3nSB/6guiKYL4kd1QTZdkD+qC2HThfBH"
    "dSFquhD9oC6EqO5CdSr8+NHcMkvNSj//XO66rP9JZVP1yJHcAkb/7EO1uo/rJ2obtLuHaRfdm5hVCzuP1eo7oP6ipoXmZqeud6cf"
    "/u3jeqBOfak1VjNV3jtt9r/djbuV//9Xd4XPhX31T9ocmczkawdt/WE8eca21+XQy5vr9pyrGtrmUVi3FAx5s8Dvbz98cOx3BZR2"
    "v91VtT5g21m7LrC1Ad/0hRv79vrt+63Vm4H97tZkexP9Mo83c+2X8ol2ykKuoecdI+4b9EQlxu52poijiGOkNU9Cg7Sx2GCMrDAY"
    "mciwRNpISiVtqGUSE8oEkShKkl1nh/84qs+G3TVyz7BBpqY5MO0nNVvZyyxLs3HQhvf37qIGhb/3CsptuSqflrefYxsokIDFaOGv"
    "Un9xt7kLe+cvE1fESw1weTO6vnn/5uPF7eji/du3V7ejT+e/Xb05v716/85dlYXyINjxDJYC5L3sno+GqgIB3V3wYD7Ncx/tvd6Z"
    "4+aibfXKy1cVf14/qY4ToTUXs4XJmjbo370NVAISVgavpzMfWg5yC8rARZ0vcgCy1U3bUvFMytu37oKwzeZTmMTp3y3UBt1UR3Wt"
    "Fq2aAPVr3NDK+K/lKgb9cg8zmQMVUHx25vRLbrMv/vp7lpXB84XV92UUWr4C/Z7naRaUWrY5mi0DxXxIfKkuQen82R4bty2UfRl1"
    "pxgs0Cqz1cDtt3u18vGL6cKOqtHHKwNT6VVeCrw9S+8eys7rsolU6xV01hwdjKm3ndWSMaJjJk6gJMHk2We1bvxbT2qT+srBD49R"
    "a49Pa4t1kDAlD07yhVrm92lxCAqvdt68/Y7z9sPeP1YL66DMSs1e9X7z9j60Q8xsGbvW4IhXpr5tostkIRPPo9B6pzfVMXlZ0E96"
    "B1M6Tgfp3QYqXUyG06APr4Qub+rmdqdrkdxqySlHWiCVaGI4jYlMosQgjkFL0CRhAuswsRIJigyJY8uFlFyEnOjk6KUYtR3yXmD1"
    "douWBljqLEnXBGyQ6xoFr4IrFV1M57Zvi+dW5aCp/c2bNQVexYSsm61ScXvr1Sj2M3jps6/4wgqIwFhd4hYTOBtS3dop8WvH7NeW"
    "ZmG/Bk1sFDgtL7xA9he4/f8B3FJEMQHxxIZYjBRJXFyk2+lglLJQxqG1WIEUMxB3YWwUc8wSBqKPeKwlMXvEZ7XCV9+Pa9jbqVZw"
    "I6cuuYLjdC8HxwFFqIrbb+KuQJHqaV7mdGhzADSZiFzkVQkyK0Tp9PDM3YwDZfYwKtIRgKcAdOQynTpvsJXb/CR4l7bysz26/9Vm"
    "awdqCsecn0iKRYh/9uj+v5DTX8jpkMhpy25cM5fPRky/Ny3sBEmhYITjJBJcJ0qQBAnLDBGMK8xtyCLECYuU1JpKRhOiEUfGWquE"
    "TcD38irimSDpa6dzfXTU7fbOXbym+nfs44HVP3VJfmrRLaFJzf/lVfnl/UNeObbltl3FPPtt3zVdP9zOXW92tsOTx4vUGOSRUi3S"
    "eKRQH0t8T8ESLLQlB4EMSYyBnTWSVlPGYk2pAvtmVCyJBovNuE0iFGEweYoboq2y8ENhY6kwhnHyvJDu389vLn99//HDZkR3Pv22"
    "FrH9yCbSIv0a3Ksv1R324Os9YPJmH7vB1KfV5tVpvUfVDfzu3JNvor3zx8K9X23Ctt+LZ3xM0AnCTIbRX6jhL9TwF2p4BdSwBCYG"
    "8bVlnrTnb7FUzTyRK1BoQ0E/4Diy0iJJLUlkZEUSx+BsWG0YISGWCQ5pZJQhikkTMizcTiz8jJ4MqWNroGFtdBu7KWu97mIGl4Bv"
    "6fLWHQduAn2QiFMIxz6FjFvd4Pp90OQjOa7zqQV1urqyeAAc2mb16/dnMx+gSw5TTX/Q61yDCgCqVHV8rj/owjJL59PcpaTUdros"
    "qvR9/ozkq5qWeU6bPDut/XtOHr9llccvXy2XM5cfqZyZcW+Cuu8NzMVDmbvJZuPufK3l/Kuq7mXXNxZ3r3wtlmDDJeEGgb+qFFHg"
    "5jLOSCzAegE/EiYTaS3HEukkiSJLBMJaRUmcaEXICzL8AeuuFsYd0mJ0HFI8ysBWe5QJQvw58KwFvroLvzyrnvkVanIFeR0A3OQ4"
    "rjqjmT0EM3DIZzNXpzXh1T5Bbd27Nr1k2GrXoZc/cHc6l9eas2323dlzMPEnkZQy+ulz/tW8e/i0f2uUXj3z35ouOK11QY/sIBRT"
    "F36XTLN5lYP7FXHGdoU2SZf5pLBqPoCdfwpQbbPtZS7hUZlK+Nmm/ca3clM2stOyEypYHIJmiBC2IWgBBHZeChMZjSOBaMQEMomk"
    "INskYQbUhmbEcBIpHtPQqqcsO12z7P2xrRv2tT5vpKCjbQq6KlcuyMVnf0afVJnim2zvdWbdKul9HTMKTGGLYlbf2XEcAgu1kUqu"
    "n6++df6LezvdAAAAFBKo7it2ocCm8d//zKNMK11jl3H5YJL18qltlPFJrrtl6uzztd1um+m99PVMqku40KS/qyZpe+1HStXd2DeX"
    "W59F9oEHOJJMscjdwENJxEPEEnBok1ACG0vtzgcJCxGKLY4FU0IpAWwdUcCqRsfIfEcc/dHBSG7PqEup23MH91v8HFHlz02e/j8x"
    "Efw2AuvicFr9f1JMwfYUedexe07Q2qNB6w3xVkLdKF8QJr8zTO7m8vbjzbvJ+cfbXyfwu06A5qLkttm02hI/25x9qBvYacpsIpFk"
    "nBMeY0mkwiCAKDHaKMHBXw1tGAoRxlpZElHKInBfSQxyJSQ8kGXi1H2c1LztUd+Kdbq67pkCCDCpS4Z+Wv4Kyk8dNL5qA1impsJH"
    "Qf2oKer3sp0UtR5tU8Z/DqXvuz7qrH5oG9/qp/YymjZUsmn+2XfDHUNWp/zPdl/Rk+5rM2m7XdjOvO32Y5vBvoNZ6harJammtLfJ"
    "6rDCPtYKIY3BGmAcugREFlMeWUliwF8JeGRIYbfJGick4iyKY4sERkIzCS6ZoQnF8TOs1auR3G6tmBhzdBJRgclPv9dbMcvrf6xk"
    "h6PUPH59isO5t4d1O3+0D/hkovJ1jQNdyMA7dDbH8fOuZOV1vVGVfPXFVrRKy/zIXTrEYm5EzLHWDtuyKBEutEYppJBlzCrCLI+Q"
    "iXRirbYazCq8Dg2PdGzEk58+4zuMaTPCXUa17fnOk+I62/fT58Tn12u5yY/XcpL3E5A7W1va36rWqMlntc3KtqfEa51/3Uxq59e9"
    "FOBbMmxf75X1+7rK+l2V6yT/3nJu3B+Yy+D2LMPZWfa9Qh+40rGkGNkkkqFAVDFgWJpYFoEzJqwWPBQM0J5NeMi1dp85MYbRJMQR"
    "EfEz06ZxhBznNHCoZqFuzrSWs05rjuokoS9PZrlPv9bdxHXArTwqru/1G58ov8uEcZp+braUu+nqj4NqmW+qEwVoJ9VlYvtRupg9"
    "nLXfp3GuQuY2Bx5O1dxvUjfpwpvkals+HvNa0737oJidcEFB2/z0ecGXHcx6+AThHWo/U6bwnd7ph4/X179dXd5Mrt59mpxfd1N0"
    "v262mJ2EyFCE6FCE2FCE+FCExFCE5FCEwqEIRQMRevWkazsJDaUZ8FCaAQ+lGfBQmgEPpRnwUJoBD6UZ8FCaAQ+lGchQmoEMpRnI"
    "UJqBDKUZyFCagQylGchQmoEMpRnIUJqBDKUZ6FCagQ6lGehQmoEOpRnoUJqBDqUZ6FCagQ6lGehQmoEOpRnYUJqBDaUZ2FCagQ2l"
    "GdhQmoENpRnYUJqBDaUZ2FCagQ2lGTh69OpKmhWj+hvZo/Zi3UuO4aDJN1WL3dv1O87jIiLdR0hQaBUTiYxkkkQhM0ZGsVSxMFYj"
    "KxKWaEUjLMIEU2KsolxIRWmcxE+cx2Gy5Txux6C3ncztGMz6BU6fQqm6wKmCO9ebhb8eUlUJ4mxq7myQZOm8vpo5Ki9ONfEZzafK"
    "XZRG0bnLUr8oL8hMZzB7Lpi+H19X3Gfp6u6+Cq9zV3Ka9ty9GKh5+/ZD79kXl+sqPw6Ws1UeuGMUf+c/mdqZyes+AvX4oTzN82H+"
    "G13qdeIk2DVl7qRStdMxqqaj5pzTfoKtztjrlk7rDzKX0Se5izKC+YPuu68Ou6aXM6XLRCDuho77F+p9zYAd3B3a4j7NOwdZQbnS"
    "0ONzT6kcv5+74+bLUeYYxln4JFZu/LrKrJW7dCGFSzWl5vH0bpW66cuacyY3T03+Ehh13olxaGquFlWgY/fLzSrvfGLotHsCdlbe"
    "wAVV4Q7l3I3cJdSbfnN081XifhWZWuTuu0LVzV0g4rKZnLdJzN67Q8d9o2x2XoLtciu06a4zdZgLnhTzfO1JN76mvIfx/feRSiEb"
    "PSq9+8XSSktDHkdKJ8gf9qsENBCyiUueI6LIKSLqbwqFFEmuiGIhZlxHKrah3e/zyKOyaHmX2819eXMMuhO8sB/jIE6L+6DugIvX"
    "dYbg/c3t6M3lb1efLm/+Y3T56erN5buLy9EvV+/eXL37Py541/PTtsJvzm8vR9c3l2+uLtwvX9Sf3abuk97BhQscanuvsmKaOH2T"
    "3yvCRYA4jxBLkNaUYUs4cjnOiIiQ4IaEsRAyiTFGmAqDOJGUGsYUsxwjF5oTmrNG9trDZLdgtj4QBvOx/BuMG+oaE1GYG5gibnFI"
    "E4RZhIyyBimZGA7dICjWWlgTsQRzK2DGoFPI8OZyfI+JSzaa1B34G4fGNDdCG0k0x2FiNVEi4ZRptzCCMRlaqQhGEgqIkIHOIjLU"
    "PCKKW6KPg9aCwhQunOnzlyMmPgjqbwrmKgwjhqPYhDHMGRhrFUVxSDU0GxFN4iREiknObayMlUAgIswxDBLEEJ82pY6scoqgk5mo"
    "jPwPqsCpsyBdFWB+bXO6vkibzpTpBH3iABJG61/Uph3ltS2++pWEaOPYnN8SOqZyTOQJF0TincfmXT3hRX9UF3hcV7zoXP25RA9y"
    "8H7ge9mP3aprxOcA9+makHBABP5Yfwga9ouPX/ojBzjwWkTADro/DZFXHUHduL/858S5GsGuCKyaPb35fT2870HZd4B9YQnm7jOE"
    "kZTUSpGECWhySUHJEsooWACDsTWgusEhMBr0huYCnnOkuLAheS7YXx/uDqS/MYxDwPwG+dSp8NTML0+LeftQdx2zriH9dGnrXm5E"
    "b/2TcwPyMma8GwnedOG4cQz6rxbpPzdYvj8p+wD578fmdtfgyyH/vCj9n6YLPVs5cQsAio3e37xxhd3PN799akvn/3xwPN8C8hLM"
    "5+ljoL2E9+tMs/8Vye3CuQ+QjyhVglnATUmsUGwUZTHTiUEWsAhXAM6kQJIRyQUWJEzCyO3yGAx4hdNQm9cC8i/tx7gHxxvs3Yfp"
    "1+c3l+9uO0Ce7EDn/b6tw/SIojDRoXKfWMDwJJLaaooswhEoWvcdaUVoEgNeE0rAGIyOQDvLJLKAfknSRvSX8fwBAOIIAVjVicuc"
    "Q2CkUCuSWEdxwhNQ4FIJHDMTJTZOwC0AVa5JEoVCwf+jODoLSoRcXSwOarMRmBAmjUilENWMcgoYXyRGGU2JhV865jIOOY1Dxgni"
    "VNqEaQNWRVqwLyHXfv6qENdAUxMqGcmQE86YFRQcBWZoiK0kCcUh4FWBE2GYiIlhiFJKVAzOBcFaGIoAcF+UsLrJSDAOJFiEFoM7"
    "wYfmgyz9mh8HlOAunO6oEF+QclkVlJiu7aMcHYzLtyNtJseUnUQMCYZfgLQ3JfnQMHuT4l8Y+y+M/SMx9p+OKlCobAhYugqffnw3"
    "+uXy3cWvb89v/m10PvqEJBqBMr+6/Y/RzaV76xL/54A25srJhku16mRjVaSlj549nJQCMekEHX7xlVwPmjg4cL25BJUtotjqhMdS"
    "EMYwsUSwKIlA3SaMxqA9QeFLl+bMGkqFlBgzbCl1PvlqabxZ7ysKcYvIGBOvKDgUl42icGGRuTOfPgAGu18NCO+BjkpcYTWS6Wzm"
    "vyYPU1Sa+FLkVToqG6nvrHutoHI/F1UszEMOeGYNJ258zSSzHd17FpjUY9G7lc3zKqLTgTI3cW2AAMBMH+HZRHc2cTa1uYstoDGf"
    "pXY1d38/d0a68/EDz+BtLBCu9Ph8e0whES5Xv8tAQWkzmS07+Xo7l8O3gPkY7BIVEQl51QLwn6dNdi9DJ7fB968I2bYi5ZvWJ2pm"
    "rl6d1jg64H95Mbr4+OH21P/nl+ug7sXm8lWJu5uDh/5qry0HCGw89R96sA5AWBcqVQKna5+MKvV7YmVisnKnq4wir2LlG09iF5uY"
    "dBXP7KjMedREmphga86s3dxAHuEGl22IjDk94YRElLyQGyhCa9xAn8sN60lbdnAH3ckd9BHuOO6va+mNdlOltOxTL3U/HK5w3wVZ"
    "lB/cyJpMKC7bdJsLpYmKKwOGti3yiQ9Xarae1SxPA+W1WhA71es+PeI7CgzpoxubeO9t60wfX2fGxoyehIC15HPW2XEKHqPoRMA6"
    "02htndnmOndCuNcSlO1YS7ZzLZlzmd3c5YA6wRN3k+ek7LgJGRuVYa/Ha1GFbvOhLnJ6/X5D3kGwprPderfU5+qLms4USOJZGckP"
    "S98LEwvS2D3sqJBqpWtf3Kel81/OseVegPJJ/6svX5V25RH5ZY+tKywJH1N0woQUIX3huoLzsraufPe6NhGGO5aTb1vO9bjN7TPu"
    "Jq+J9TxbF75eEOBpveCnncjRrty1GRROgstarTeJC9xqbc0vWHbE754ARltUW0DOmJQJi2DE1Yd/Hlk5/tjK+W/muHMGgsJQPGfl"
    "SLVyHOqHbG3lxObKbc8c/ehHN9qX7Zfydi242Lbga19j2/hqko8kdmjJe/XNB5+2fbEpmWZ59xMd7lMaRfupJ2CUNne0W9cmafJ5"
    "03W/qB4w71408fiisdBNOWMiEuSZi+YspjOXIcVriyZfYdF2rI78H7Q6vtlHF0E+tgggNsRJDhWMYvaiRZAI03XMEj7iSPjcol+c"
    "Mdg1z+FOKxYGC2vNFjhy0zTa0T6lcTi/2Yh6LsNM812B9DqdzaxHCiP/RctulHS9D7QN1FStnZppvlxVX0XT4NqtqdOTFmK7Hfnq"
    "CKHDBmn35KAB2rWePw7iVdG3ns0eR7z0N4bu7jL/ITv/kTh336iy/Q5juVE0c9XYjmqumjtpAKv2stMlIps2qQT8Nv5u3gx38ia/"
    "BU6idIzAVUUi4uxlWj3iRPZ5E5Pt9njHltTuM+F15m1290f+8ag51eqxsPu8StcXXrO0wBDaujQcMzCVbZQ/vGlOrOrsgL6zp53b"
    "XP2DrMYl9mcup82RS7vm7V5E79pWD9efvtl6W+ysj8/coce/rB+QHMMDq4rmc4d1v1v5zIO/2yw9LvHcTLsbELY/O20CgNZ97yVt"
    "2sJceN1ZK7/02tvrmRQuR7Ar8n17JmBoGnDw55//6/8BgoTbpA=="
)


def _workspace(tmp_path: Path) -> EntityResolutionWorkspace:
    return EntityResolutionWorkspace.create(RunContext("RUN", tmp_path / "run"))


def _state_hash(state: dict[str, object]) -> str:
    payload = dict(state)
    payload.pop("state_hash", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256((encoded + "\n").encode("utf-8")).hexdigest()


def _materialize_g5_legacy_state(tmp_path: Path) -> tuple[RunContext, dict[str, object], Path]:
    """Copy the actual 13-domain G5 legacy state into an isolated fixture.

    The compressed payload is a checked-in forensic copy captured before the
    repair. The test never loads or writes an active benchmark run.
    """

    source_bytes = zlib.decompress(base64.b64decode(_G5_LEGACY_STATE_ZLIB_B64))
    assert hashlib.sha256(source_bytes).hexdigest() == _G5_LEGACY_STATE_SHA256
    legacy = json.loads(source_bytes.decode("utf-8"))
    assert legacy["schema_version"] == "auto_foundry.entity_resolution.v1"
    assert len(legacy["domains"]) == 13

    run_root = tmp_path / "g5-legacy-copy"
    entity_root = run_root / "entity_resolution"
    (entity_root / "domains").mkdir(parents=True)
    (entity_root / "committed").mkdir()
    state_path = entity_root / "state.json"
    state_path.write_bytes(source_bytes)
    context = RunContext(legacy["run_id"], run_root, (tmp_path,))
    return context, legacy, state_path


def test_actual_g5_legacy_state_without_published_artifacts_fails_closed(
    tmp_path: Path,
) -> None:
    context, legacy, state_path = _materialize_g5_legacy_state(tmp_path)
    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns
    assert any(domain.get("state") == "ready" for domain in legacy["domains"].values())
    with pytest.raises(ValueError, match="published artifact chain is missing"):
        EntityResolutionWorkspace.load(context)
    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime


def test_malformed_near_legacy_state_fails_without_mutation(tmp_path: Path) -> None:
    context, legacy, state_path = _materialize_g5_legacy_state(tmp_path)
    malformed = json.loads(state_path.read_text(encoding="utf-8"))
    malformed["domains"]["customer"]["unexpected_field"] = "do-not-migrate"
    malformed["state_hash"] = _state_hash(malformed)
    state_path.write_text(
        json.dumps(malformed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns
    with pytest.raises(ValueError, match="legacy identity domain reservation fields are invalid"):
        EntityResolutionWorkspace.load(context)
    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime
    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == legacy["schema_version"]


def test_invalid_legacy_domain_value_fails_before_upgrade_persist(tmp_path: Path) -> None:
    context, _, state_path = _materialize_g5_legacy_state(tmp_path)
    malformed = json.loads(state_path.read_text(encoding="utf-8"))
    malformed["domains"]["customer"]["discovered_by_item_id"] = None
    malformed["state_hash"] = _state_hash(malformed)
    state_path.write_text(
        json.dumps(malformed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before_bytes = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns
    with pytest.raises((TypeError, ValueError), match="(item_id|discovered_by_item_id)"):
        EntityResolutionWorkspace.load(context)
    assert state_path.read_bytes() == before_bytes
    assert state_path.stat().st_mtime_ns == before_mtime


def _accepted_result() -> EntityResolutionResult:
    decision = IdentityDecision(
        candidate_id="candidate-1",
        decision="same_object",
        decision_id="decision-1",
        review_status="accepted",
        reviewer_ref="independent-reviewer",
        evidence_refs=("evidence-1",),
    )
    mapping = CanonicalMapping(
        canonical_id="customer-1",
        object_type="customer",
        source_identities=("account-a", "account-b", "account-c"),
        decision_id="decision-1",
    )
    return EntityResolutionResult(
        ontology_items=(OntologyItem(item_id="customer", item_type="entity", label="Customer"),),
        identity_decisions=(decision,),
        canonical_mappings=(mapping,),
        representation_relationships=(
            {"relationship_id": "customer-representation", "source_id": "customer", "target_id": "customer-1", "relationship_type": "represents"},
        ),
        coverage={"source_count": 3, "mapped_count": 3},
        population={"source_ids": ["account-a", "account-b", "account-c"]},
        exceptions=("unresolved-account",),
        metadata={"pattern": {"rule": "owner-supplied"}},
        evidence_refs=("work/evidence.jsonl#evidence-1",),
        script_receipt_refs=("script_receipts/receipt-1.json",),
        source_hash=SOURCE_HASH,
    )


def _accepted_result_for(prefix: str) -> EntityResolutionResult:
    """Build a collision-free result for multi-domain integrity fixtures."""
    result = _accepted_result()
    decision = replace(
        result.identity_decisions[0],
        candidate_id=f"{prefix}-candidate",
        decision_id=f"{prefix}-decision",
    )
    mapping = replace(
        result.canonical_mappings[0],
        canonical_id=f"{prefix}-mapping",
        decision_id=decision.decision_id,
    )
    ontology = replace(result.ontology_items[0], item_id=f"{prefix}-customer")
    relationship = dict(result.representation_relationships[0])
    relationship.update(
        {
            "relationship_id": f"{prefix}-representation",
            "source_id": ontology.item_id,
            "target_id": mapping.canonical_id,
        }
    )
    return replace(
        result,
        ontology_items=(ontology,),
        identity_decisions=(decision,),
        canonical_mappings=(mapping,),
        representation_relationships=(relationship,),
    )


def test_resolution_result_reuses_and_extends_existing_ontology_item() -> None:
    model = LivingEnterpriseModel(run_id="RUN-ER-ONTOLOGY-MERGE")
    model.add_ontology_item(
        OntologyItem(
            item_id="erp_transactions.parquet",
            item_type="entity",
            label="ERP sales transactions",
            properties={"analytical_role": "distinct-sales-document customer activity", "columns": ["SALESDOCUMENT"]},
            source_refs=("erp_transactions.parquet",),
            effective_period="2019-07-06/2020-06-29",
        )
    )
    result = EntityResolutionResult(
        ontology_items=(
            OntologyItem(
                item_id="erp_transactions.parquet",
                item_type="entity",
                label="ERP transaction rows",
                properties={
                    "analytical_role": "ERP line-level reference target",
                    "columns": ["SALESDOCUMENT", "SALESDOCUMENTITEM"],
                    "row_count": 1916685,
                },
                source_refs=("erp_transactions.parquet", "erp_transactions.schema.json"),
                limitations=("Line-level fan-out",),
                effective_period=None,
            ),
        ),
        source_hash=SOURCE_HASH,
    )

    EntityResolutionWorkspace._apply_result(model, result)
    item = model.ontology["erp_transactions.parquet"]
    assert len(model.ontology) == 1
    assert item.label == "ERP sales transactions"
    assert item.properties["analytical_role"] == "distinct-sales-document customer activity"
    assert item.properties["columns"] == ["SALESDOCUMENT", "SALESDOCUMENTITEM"]
    assert item.properties["row_count"] == 1916685
    assert item.source_refs == ("erp_transactions.parquet", "erp_transactions.schema.json")
    assert item.effective_period == "2019-07-06/2020-06-29"
    assert item.limitations == ("Line-level fan-out",)


def test_resolution_result_reuses_existing_relationship_before_endpoint_validation() -> None:
    model = LivingEnterpriseModel(run_id="RUN-ER-RELATIONSHIP-ID-FIRST")
    model.add_ontology_item(OntologyItem(item_id="source", item_type="entity", label="Source"))
    model.add_ontology_item(OntologyItem(item_id="target", item_type="entity", label="Target"))
    model.add_relationship(
        {
            "relationship_id": "source-target",
            "source_id": "source",
            "target_id": "target",
            "relationship_type": "represents",
        }
    )
    result = EntityResolutionResult(
        representation_relationships=(
            {
                "relationship_id": "source-target",
                # Existing edge IDs are authoritative even when a duplicate
                # result omits endpoints and carries malformed shape fields.
                "analysis_relationship_id": "malformed-analysis",
                "join_keys": "not-a-list",
            },
        ),
        source_hash=SOURCE_HASH,
    )

    EntityResolutionWorkspace._apply_result(model, result)

    assert set(model.relationships) == {"source-target"}
    assert model.relationships["source-target"]["source_id"] == "source"
    assert model.relationships["source-target"]["target_id"] == "target"
    assert model.relationships["source-target"]["relationship_type"] == "represents"


def test_two_resolution_domains_replay_shared_ontology_and_relationship_ids(tmp_path: Path) -> None:
    context = RunContext("RUN-ER-SHARED-SEMANTICS", tmp_path / "run")
    workspace = EntityResolutionWorkspace.create(context)

    def result_for(domain: str, *, richer: bool) -> EntityResolutionResult:
        decision = IdentityDecision(
            candidate_id=f"candidate-{domain}",
            decision="same_object",
            decision_id=f"decision-{domain}",
            review_status="accepted",
            reviewer_ref=f"reviewer-{domain}",
        )
        mapping = CanonicalMapping(
            canonical_id=f"mapping-{domain}",
            object_type="customer",
            source_identities=(f"source-{domain}",),
            decision_id=decision.decision_id,
        )
        source = OntologyItem(
            item_id="shared-source",
            item_type="entity" if not richer else "representation",
            label="Canonical source" if not richer else "Requirement wording",
            properties={
                "columns": ["id"] if not richer else ["id", "created_at"],
                "date_field": None if not richer else "created_at",
                "nested": {"first": True} if not richer else {"second": True},
            },
            source_refs=("shared.csv",) if not richer else ("shared.csv", "schema.json"),
            limitations=("Initial limitation",) if not richer else ("Second limitation",),
            effective_period="2020" if not richer else None,
        )
        target = OntologyItem(
            item_id="shared-target",
            item_type="entity",
            label="Canonical target",
        )
        return EntityResolutionResult(
            ontology_items=(source, target),
            identity_decisions=(decision,),
            canonical_mappings=(mapping,),
            representation_relationships=(
                {
                    "relationship_id": "shared-relationship",
                    # The second domain intentionally carries malformed
                    # endpoint/shape details.  The relationship ID is
                    # already canonical, so replay must retain the first
                    # edge without validating this incoming duplicate.
                    **({} if richer else {"source_id": "shared-source", "target_id": "shared-target"}),
                    "relationship_type": "represents" if not richer else None,
                    "join_keys": "not-a-list" if richer else None,
                },
            ),
            coverage={"source_count": 1, "mapped_count": 1},
            population={"source_ids": [f"source-{domain}"]},
            source_hash=SOURCE_HASH,
        )

    # Commit in reverse lexical order.  The first committed scalar remains
    # canonical while the later domain contributes only additive facts.
    for domain, richer in (("z-domain", False), ("a-domain", True)):
        workspace.reserve_identity_domain(
            domain,
            "customer",
            f"REQ-{domain}",
            "shared semantic publication",
            source_hints=(f"{domain}.csv",),
        )
        workspace.claim_resolution_owner(domain, f"owner-{domain}")
        workspace.submit_result(
            domain,
            f"owner-{domain}",
            result_for(domain, richer=richer),
            expected_scope_hash=workspace.current_scope(domain).scope_hash,
        )
        workspace.record_review(domain, "accept", f"independent-{domain}")
        workspace.commit(domain)

    # Loading the authoritative entity-resolution ledger must accept the two
    # domain-local audit rows even though their semantic object IDs repeat.
    reloaded = EntityResolutionWorkspace.load(context)
    assert {domain.state for domain in reloaded.domains()} == {"ready"}
    first_replay = replay_ready_commits(context, LivingEnterpriseModel(run_id=context.run_id))
    second_replay = replay_ready_commits(context, LivingEnterpriseModel(run_id=context.run_id))
    assert first_replay.export() == second_replay.export()
    assert set(first_replay.ontology) == {"shared-source", "shared-target", "shared-relationship"}
    assert set(first_replay.relationships) == {"shared-relationship"}
    source = first_replay.ontology["shared-source"]
    assert source.item_type == "entity"
    assert source.label == "Canonical source"
    assert source.properties["columns"] == ["id", "created_at"]
    assert source.properties["date_field"] == "created_at"
    assert source.properties["nested"] == {"first": True, "second": True}
    assert source.source_refs == ("shared.csv", "schema.json")
    assert source.limitations == ("Initial limitation", "Second limitation")
    assert source.effective_period == "2020"
    assert first_replay.relationships["shared-relationship"]["relationship_type"] == "represents"
    # Requirement-mode projection can replay run-level resolution commits
    # without item-local integrations; materialize the empty lifecycle after
    # publishing so reservation remains independent of a missing item root.
    RunLifecycle.create(context, (), mode="requirement")
    projection = LivingEnterpriseModelProjector.project(context)
    projected = projection.model
    assert projected.export() == first_replay.export()
    assert {binding["domain_id"] for binding in projection.resolution_bindings} == {
        "z-domain",
        "a-domain",
    }
    # Resolution authority is cumulative; the projection API no longer has an
    # exclusion parameter that could return bindings for unapplied semantics.
    assert not any(
        name.startswith("exclude")
        for name in inspect.signature(LivingEnterpriseModelProjector.project).parameters
    )


def test_equal_timestamp_replay_order_uses_deterministic_domain_tie_break() -> None:
    timestamp = "2026-01-01T00:00:00+00:00"
    grouped = {
        "z-domain": {
            "batches": (
                ({"committed_at": timestamp, "revision": 1, "manifest_hash": "z" * 64}, ()),
            ),
        },
        "a-domain": {
            "batches": (
                ({"committed_at": timestamp, "revision": 1, "manifest_hash": "a" * 64}, ()),
            ),
        },
    }

    replay_order = [
        (domain_id, manifest["revision"])
        for domain_id, manifest, _records in _iter_replay_batches(grouped)
    ]
    assert replay_order == [("a-domain", 1), ("z-domain", 1)]


def test_capacity_and_idempotent_recovery_leases(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot exceed total_active"):
        ResolutionCapacity(total_active=1, entity_resolution=2, analytical_owner=1, specialist=1)
    context = RunContext("RUN", tmp_path / "run")
    workspace = EntityResolutionWorkspace.create(
        context,
        capacity=ResolutionCapacity(total_active=2, entity_resolution=1, analytical_owner=1, specialist=1),
    )
    first = workspace.claim_worker("entity_resolution", "owner", "domain")
    assert workspace.claim_worker("entity_resolution", "owner", "domain") == first
    with pytest.raises(ValueError, match="already leased"):
        workspace.claim_worker("entity_resolution", "other", "domain")
    workspace.release_worker(first, recovery=True)
    assert workspace.active_resolution_count == 0
    assert EntityResolutionWorkspace.load(context).capacity.total_active == 2


def test_resolution_domain_leases_do_not_duplicate_coordinator_capacity_checks(tmp_path: Path) -> None:
    """Domain ownership remains exclusive while Coordinator owns physical caps."""

    context = RunContext("RUN-ER-CAPACITY-AUTHORITY", tmp_path / "run")
    workspace = EntityResolutionWorkspace.create(
        context,
        capacity=ResolutionCapacity(total_active=1, entity_resolution=1, analytical_owner=1, specialist=1),
    )
    for domain_id in ("domain-1", "domain-2"):
        workspace.reserve_identity_domain(domain_id, "customer", "REQ-1", "shared identity domain")

    first = workspace.claim_resolution_owner("domain-1", "resolution-owner-1")
    second = workspace.claim_resolution_owner("domain-2", "resolution-owner-2")
    assert first.subject_id == "domain-1"
    assert second.subject_id == "domain-2"
    assert len(workspace.active_leases) == 2
    with pytest.raises(ValueError, match="total active worker capacity"):
        workspace.claim_worker("specialist", "specialist-owner", "task-1")


def test_reserve_review_commit_and_replay(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _workspace(tmp_path)
    reservation = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "Q-001",
        "source accounts require reviewed identity resolution",
        source_hints=("accounts.csv",),
        representation_item_ids=("customer-source",),
    )
    assert workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "Q-001",
        "source accounts require reviewed identity resolution",
        source_hints=("accounts.csv",),
        representation_item_ids=("customer-source",),
    ) == reservation
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    advisory = workspace.mapping_completeness_advisory()
    assert len(advisory) == 1
    assert advisory[0].status == "available"
    assert advisory[0].canonical_mapping_count == 1
    assert advisory[0].mapped_source_identity_count == 3
    assert advisory[0].unresolved_record_count == 0
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    assert workspace.get_domain("customer-domain").state == "review_pending"
    commit = workspace.commit("customer-domain")
    assert commit.record_count == 4
    model = replay_ready_commits(context, LivingEnterpriseModel(run_id=context.run_id))
    assert set(model.ontology) == {"customer", "customer-representation"}
    assert set(model.identity_decisions) == {"decision-1"}
    assert set(model.canonical_mappings) == {"customer-1"}
    assert set(model.relationships) == {"customer-representation"}
    committed_root = (workspace.root / commit.manifest_path).parent
    assert {path.name for path in committed_root.iterdir()} == {"manifest.json", "records.jsonl"}


def test_committed_domain_additive_revision_preserves_v1_and_merges_facts(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-LINK",))
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    workspace.claim_resolution_owner("customer-domain", "owner-v1")
    v1_result = _accepted_result()
    workspace.submit_result(
        "customer-domain",
        "owner-v1",
        v1_result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v1")
    v1 = workspace.commit("customer-domain")
    v1_manifest = (workspace.root / v1.manifest_path).read_bytes()
    v1_records = (workspace.root / v1.records_path).read_bytes()

    # An accepted item relationship may depend on an endpoint published by
    # the predecessor revision.  Candidate validation keeps the full
    # projection while the integration edge is published independently.
    item = ItemWorkspace.create(context, "REQ-LINK", original_text="customer link")
    item.bind_analysis_owner("analytical-owner")
    item.write_plan({"item_id": "REQ-LINK", "offline": True})
    item.write_draft({"answer": "customer link"})
    relationship = {
        "record_kind": "analytical_relationship",
        "relationship_id": "accepted-customer-link",
        "source_id": "customer",
        "target_id": "customer-1",
        "cardinality": "one_to_one",
        "join_keys": [{"source_field": "id", "target_field": "id"}],
        "matched_pairs": 1,
        "source_population": 1,
        "target_population": 1,
        "matched_source_count": 1,
        "matched_target_count": 1,
        "source_coverage": 1.0,
        "target_coverage": 1.0,
        "date_authority": "fixture-controlled snapshot",
        "as_of": None,
        "limitations": ["synthetic fixture"],
        "evidence_refs": ["work/plan.json"],
        "publishable": True,
        "no_relationship_reason": None,
        "audit_id": None,
        "owner_ref": "analytical-owner",
    }
    item.append_analytical_relationship(relationship)
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-REQ-LINK",
    )
    session.add_relationship(
        {
            "relationship_id": "accepted-customer-link",
            "analysis_relationship_id": "accepted-customer-link",
            "source_id": "customer",
            "target_id": "customer-1",
            "cardinality": "one_to_one",
            "join_keys": [{"source_field": "id", "target_field": "id"}],
            "matched_pairs": 1,
            "source_population": 1,
            "target_population": 1,
            "matched_source_count": 1,
            "matched_target_count": 1,
            "source_coverage": 1.0,
            "target_coverage": 1.0,
            "date_authority": "fixture-controlled snapshot",
            "as_of": None,
            "limitations": ["synthetic fixture"],
            "evidence_refs": ["work/plan.json"],
        },
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    assert session.validate().valid
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()

    # A material AO proposal opens revision 2 without replacing the v1
    # authority.  The same-domain v2 result adds reviewed structured facts;
    # repeated identity/mapping/relationship IDs remain canonical no-ops.
    expanded = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-2",
        "returns representation requires additive identity scope",
        source_hints=("returns.csv",),
        representation_item_ids=("returns-customer-id",),
    )
    assert expanded.domain_id == "customer-domain"
    assert expanded.revision == 2
    assert expanded.published_revision == 1
    assert expanded.commit_manifest_hash == v1.manifest_hash
    assert workspace.get_domain("customer-domain").state == "reserved"
    assert set(replay_ready_commits(context).ontology) == {"customer", "customer-representation"}

    workspace.claim_resolution_owner("customer-domain", "owner-v2")
    discovered = workspace.record_scope_discovery(
        "customer-domain",
        "owner-v2",
        source_hints=("returns-detail.csv",),
        representation_item_ids=("returns-detail-id",),
    )
    assert discovered["status"] == "added"
    v2_result = replace(
        v1_result,
        ontology_items=(
            replace(
                v1_result.ontology_items[0],
                label="Customer wording from returns requirement",
                properties={
                    "columns": ["customer_id", "return_id"],
                    "key": "customer_id",
                    "row_count": 3,
                },
                source_refs=("returns.csv",),
                limitations=("returns extract is partial",),
            ),
        ),
        representation_relationships=(
            {
                "relationship_id": "customer-representation",
                # The requirement-local audit payload may be incomplete or
                # differently worded; the committed edge remains canonical.
                "source_id": "unknown-source",
                "target_id": "unknown-target",
                "relationship_type": "returns-representation",
            },
        ),
    )
    workspace.submit_result(
        "customer-domain",
        "owner-v2",
        v2_result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v2")
    v2 = workspace.commit("customer-domain")
    assert v2.revision == 2
    assert v2.supersedes_manifest_hash == v1.manifest_hash
    assert v2.scope_hash == workspace.get_domain("customer-domain").published_scope_hash
    assert (workspace.root / v1.manifest_path).read_bytes() == v1_manifest
    assert (workspace.root / v1.records_path).read_bytes() == v1_records

    current = replay_ready_commits(context).export()
    customer = current["ontology"][0]
    assert customer["label"] == "Customer"
    assert customer["properties"] == {
        "columns": ["customer_id", "return_id"],
        "key": "customer_id",
        "row_count": 3,
    }
    assert customer["source_refs"] == ["returns.csv"]
    assert customer["limitations"] == ["returns extract is partial"]
    assert current["canonical_mappings"][0]["source_identities"] == [
        "account-a",
        "account-b",
        "account-c",
    ]
    assert current["relationships"]["customer-representation"]["source_id"] == "customer"
    assert current["relationships"]["customer-representation"]["target_id"] == "customer-1"
    assert EntityResolutionWorkspace.committed_bindings(context)[0]["revision"] == 2
    projected = LivingEnterpriseModelProjector.project(context).model.export()
    assert "accepted-customer-link" in projected["relationships"]


def test_waiters_are_bound_to_required_revision_across_late_additive_publish_and_failure(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-V1",
        "initial identity request",
        source_hints=("customers.csv",),
    )
    workspace.mark_waiting_on_resolution(
        "REQ-V1",
        ("customer-domain",),
        "awaiting v1",
        owner_ref="ao-v1",
    )
    workspace.claim_resolution_owner("customer-domain", "owner-v1")
    result = _accepted_result()
    workspace.submit_result(
        "customer-domain",
        "owner-v1",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v1")
    workspace.commit("customer-domain")

    # Open v2 before the original v1 wait is polled.  Each request keeps its
    # first-admission boundary; the exact retry must not churn its bytes.
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-V2",
        "wider identity scope",
        source_hints=("returns.csv",),
    )
    workspace.mark_waiting_on_resolution(
        "REQ-V2",
        ("customer-domain",),
        "awaiting v2",
        owner_ref="ao-v2",
    )
    before_retry = workspace.state_path.read_bytes()
    workspace.mark_waiting_on_resolution(
        "REQ-V2",
        ("customer-domain",),
        "awaiting v2",
        owner_ref="ao-v2",
    )
    assert workspace.state_path.read_bytes() == before_retry
    statuses = workspace.requirement_runtime_statuses()
    assert statuses["REQ-V1"]["state"] == "ready_to_resume"
    assert statuses["REQ-V2"]["state"] == "waiting_on_resolution"
    assert statuses["REQ-V1"]["required_revisions"] == {"customer-domain": 1}
    assert statuses["REQ-V2"]["required_revisions"] == {"customer-domain": 2}

    workspace.claim_resolution_owner("customer-domain", "owner-v2")
    workspace.submit_result(
        "customer-domain",
        "owner-v2",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "fail", "review-v2")
    statuses = workspace.requirement_runtime_statuses()
    assert statuses["REQ-V1"]["state"] == "ready_to_resume"
    assert statuses["REQ-V2"]["state"] == "waiting_on_resolution"


def _committed_two_revision_domain(tmp_path: Path) -> tuple[EntityResolutionWorkspace, object, object]:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-V1", "initial", source_hints=("a.csv",))
    workspace.claim_resolution_owner("customer-domain", "owner-v1")
    result = _accepted_result()
    workspace.submit_result(
        "customer-domain",
        "owner-v1",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v1")
    first = workspace.commit("customer-domain")
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-V2", "expanded", source_hints=("b.csv",))
    workspace.claim_resolution_owner("customer-domain", "owner-v2")
    workspace.submit_result(
        "customer-domain",
        "owner-v2",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v2")
    second = workspace.commit("customer-domain")
    return workspace, first, second


def test_revision_manifest_path_swap_and_stale_pointer_fail_closed(tmp_path: Path) -> None:
    workspace, first, second = _committed_two_revision_domain(tmp_path)
    root_manifest = workspace.root / first.manifest_path
    child_manifest = workspace.root / second.manifest_path
    root_bytes = root_manifest.read_bytes()
    child_bytes = child_manifest.read_bytes()
    root_manifest.write_bytes(child_bytes)
    child_manifest.write_bytes(root_bytes)
    before_state = workspace.state_path.read_bytes()
    with pytest.raises(ValueError, match="revision 1 root path"):
        EntityResolutionWorkspace.load(workspace.context)
    assert workspace.state_path.read_bytes() == before_state

    workspace, first, second = _committed_two_revision_domain(tmp_path / "stale-pointer")
    with workspace._locked():
        workspace._refresh()
        entry = dict(workspace._state["domains"]["customer-domain"])
        entry["commit_manifest_hash"] = first.manifest_hash
        workspace._state["domains"]["customer-domain"] = entry
        workspace._persist()
    before_state = workspace.state_path.read_bytes()
    with pytest.raises(ValueError, match="published revision is stale|published scope is stale"):
        EntityResolutionWorkspace.load(workspace.context)
    assert workspace.state_path.read_bytes() == before_state


def test_existing_revision_manifest_wrong_source_hash_fails_cas_reconciliation(tmp_path: Path) -> None:
    workspace, _first, second = _committed_two_revision_domain(tmp_path)
    manifest_path = workspace.root / second.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_hash"] = "f" * 64
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    manifest["manifest_hash"] = hashlib.sha256(
        (json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with workspace._locked():
        workspace._refresh()
        entry = dict(workspace._state["domains"]["customer-domain"])
        entry["commit_manifest_hash"] = manifest["manifest_hash"]
        workspace._state["domains"]["customer-domain"] = entry
        workspace._persist()
    with pytest.raises(ValueError, match="source_hash does not match"):
        EntityResolutionWorkspace.load(workspace.context)


def _committed_v1_domain_with_wait(tmp_path: Path) -> tuple[EntityResolutionWorkspace, object]:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-V1",
        "initial",
        source_hints=("a.csv",),
    )
    workspace.mark_waiting_on_resolution(
        "REQ-V1",
        ("customer-domain",),
        "awaiting v1",
        owner_ref="ao-v1",
    )
    workspace.claim_resolution_owner("customer-domain", "owner-v1")
    result = _accepted_result()
    workspace.submit_result(
        "customer-domain",
        "owner-v1",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v1")
    return workspace, workspace.commit("customer-domain")


@pytest.mark.parametrize("active_v2_state", ("reserved", "failed"))
def test_missing_published_v1_artifacts_fail_closed_while_v2_is_unpublished(
    tmp_path: Path,
    active_v2_state: str,
) -> None:
    workspace, v1 = _committed_v1_domain_with_wait(tmp_path / active_v2_state)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-V2",
        "expanded",
        source_hints=("b.csv",),
    )
    if active_v2_state == "failed":
        workspace.claim_resolution_owner("customer-domain", "owner-v2")
        result = _accepted_result()
        workspace.submit_result(
            "customer-domain",
            "owner-v2",
            result,
            expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
        )
        workspace.record_review("customer-domain", "fail", "review-v2")
    published_root = (workspace.root / v1.manifest_path).parent
    shutil.rmtree(published_root)
    before_state = workspace.state_path.read_bytes()
    with pytest.raises(ValueError, match="published artifact chain is missing"):
        EntityResolutionWorkspace.load(workspace.context)
    assert workspace.state_path.read_bytes() == before_state


def test_missing_published_v1_artifacts_fail_closed_for_ready_v1(tmp_path: Path) -> None:
    workspace, v1 = _committed_v1_domain_with_wait(tmp_path)
    shutil.rmtree((workspace.root / v1.manifest_path).parent)
    before_state = workspace.state_path.read_bytes()
    with pytest.raises(ValueError, match="published artifact chain is missing"):
        EntityResolutionWorkspace.load(workspace.context)
    assert workspace.state_path.read_bytes() == before_state


def test_loaded_workspace_runtime_statuses_fail_when_published_artifacts_are_deleted(
    tmp_path: Path,
) -> None:
    workspace, v1 = _committed_v1_domain_with_wait(tmp_path)
    loaded = EntityResolutionWorkspace.load(workspace.context)
    shutil.rmtree((loaded.root / v1.manifest_path).parent)
    before_state = loaded.state_path.read_bytes()
    with pytest.raises(ValueError, match="published artifact chain is missing"):
        loaded.requirement_runtime_statuses()
    assert loaded.state_path.read_bytes() == before_state


def test_intact_v1_remains_readable_for_pending_and_failed_v2_waiters(tmp_path: Path) -> None:
    workspace, _v1 = _committed_v1_domain_with_wait(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-V2",
        "expanded",
        source_hints=("b.csv",),
    )
    workspace.mark_waiting_on_resolution(
        "REQ-V2",
        ("customer-domain",),
        "awaiting v2",
        owner_ref="ao-v2",
    )
    loaded = EntityResolutionWorkspace.load(workspace.context)
    statuses = loaded.requirement_runtime_statuses()
    assert statuses["REQ-V1"]["state"] == "ready_to_resume"
    assert statuses["REQ-V2"]["state"] == "waiting_on_resolution"

    loaded.claim_resolution_owner("customer-domain", "owner-v2")
    result = _accepted_result()
    loaded.submit_result(
        "customer-domain",
        "owner-v2",
        result,
        expected_scope_hash=loaded.current_scope("customer-domain").scope_hash,
    )
    loaded.record_review("customer-domain", "fail", "review-v2")
    reloaded = EntityResolutionWorkspace.load(workspace.context)
    statuses = reloaded.requirement_runtime_statuses()
    assert statuses["REQ-V1"]["state"] == "ready_to_resume"
    assert statuses["REQ-V2"]["state"] == "waiting_on_resolution"


def test_shared_domain_requests_attach_idempotently_without_merging_distinct_keys(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "first requirement needs identity",
        source_hints=("customers.csv",),
        representation_item_ids=("customers",),
    )
    second = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-2",
        "second requirement needs the same identity",
        source_hints=("orders.csv",),
        representation_item_ids=("orders",),
    )
    assert second.requested_by == ("REQ-1", "REQ-2")
    assert len(second.request_records) == 2
    assert workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-2",
        "second requirement needs the same identity",
        source_hints=("orders.csv",),
        representation_item_ids=("orders",),
    ) == second

    # Different canonical keys remain separate even if an operator supplies
    # the same optional identity label while aliases are still uncertain.
    alias = workspace.reserve_identity_domain(
        "customer-domain-alias",
        "customer",
        "REQ-3",
        "uncertain alias requires explicit resolution",
        canonical_identity="customer-domain",
    )
    assert {domain.domain_id for domain in workspace.domains()} == {
        "customer-domain",
        "customer-domain-alias",
    }
    assert alias.requested_by == ("REQ-3",)


def test_reopened_request_binds_generation_and_data_revision_without_reusing_ready_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    lineage = ["G-0001", "D-0001"]
    monkeypatch.setattr(workspace, "_authoritative_lineage", lambda: tuple(lineage))

    first = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
    )
    assert first.generation_id == "G-0001"
    assert first.data_revision_id == "D-0001"
    assert first.request_records[0]["generation_id"] == "G-0001"
    assert first.request_records[0]["data_revision_id"] == "D-0001"
    workspace.claim_resolution_owner("customer-domain", "owner-v1")
    workspace.submit_result(
        "customer-domain",
        "owner-v1",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v1")
    workspace.commit("customer-domain")

    # A refreshed G/D lineage can attach the same requirement again.  The
    # additional hint opens v2 and clears the ready candidate rather than
    # mutating the immutable v1 publication.
    lineage[:] = ["G-0002", "D-0002"]
    second = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv", "returns.csv"),
    )
    assert second.revision == 2
    assert second.published_revision == 1
    assert second.state == "reserved"
    assert second.result_hash is None
    assert second.request_records[-1]["generation_id"] == "G-0002"
    assert second.request_records[-1]["data_revision_id"] == "D-0002"
    authoritative = workspace.authoritative_request_for_requirement("REQ-1", "customer-domain")
    assert authoritative is not None
    assert authoritative.generation_id == "G-0002"
    assert authoritative.data_revision_id == "D-0002"
    state_before_retry = workspace.state_path.read_bytes()
    assert workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv", "returns.csv"),
    ) == second
    assert workspace.state_path.read_bytes() == state_before_retry

    with pytest.raises(ValueError, match="conflicts with prior request"):
        workspace.reserve_identity_domain(
            "customer-domain",
            "customer",
            "REQ-1",
            "mutated same-lineage request",
            source_hints=("customers.csv", "returns.csv"),
        )

    workspace.claim_resolution_owner("customer-domain", "owner-v2")
    workspace.submit_result(
        "customer-domain",
        "owner-v2",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "review-v2")
    workspace.commit("customer-domain")

    # Identical semantic bytes on a changed D are still a successor request;
    # stale v2 readiness is never reused.
    lineage[:] = ["G-0002", "D-0003"]
    third = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv", "returns.csv"),
    )
    assert third.revision == 3
    assert third.published_revision == 2
    assert third.state == "reserved"
    assert third.result_hash is None
    assert third.request_records[-1]["generation_id"] == "G-0002"
    assert third.request_records[-1]["data_revision_id"] == "D-0003"


def test_lineage_bound_request_retry_is_deterministic_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(workspace, "_authoritative_lineage", lambda: ("G-0001", "D-0001"))
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def reserve() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(
                workspace.reserve_identity_domain(
                    "customer-domain",
                    "customer",
                    "REQ-1",
                    "identity required",
                    source_hints=("customers.csv",),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - worker evidence is asserted below
            errors.append(exc)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    domain = workspace.get_domain("customer-domain")
    assert len(domain.request_records) == 1
    assert domain.request_records[0]["generation_id"] == "G-0001"
    assert domain.request_records[0]["data_revision_id"] == "D-0001"


def test_owner_scope_discovery_is_lease_bound_idempotent_and_materially_hashed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    before = (workspace.root / "state.json").read_bytes()
    initial = workspace.current_scope("customer-domain")
    assert isinstance(initial, IdentityDomainScope)

    added = workspace.record_scope_discovery(
        "customer-domain",
        "resolution-owner",
        source_hints=("customers.csv", "returns.csv"),
        representation_item_ids=("customer-id", "returns-customer-id"),
    )
    assert added["status"] == "added"
    assert added["added_source_hints"] == ["returns.csv"]
    assert added["added_representation_item_ids"] == ["returns-customer-id"]
    assert added["scope_hash"] != initial.scope_hash
    after = (workspace.root / "state.json").read_bytes()
    assert after != before

    retry = workspace.record_scope_discovery(
        "customer-domain",
        "resolution-owner",
        source_hints=("returns.csv", "customers.csv"),
        representation_item_ids=("returns-customer-id", "customer-id"),
    )
    assert retry["status"] == "already_present"
    assert retry["scope_hash"] == added["scope_hash"]
    assert (workspace.root / "state.json").read_bytes() == after

    with pytest.raises(ValueError, match="does not own"):
        workspace.record_scope_discovery(
            "customer-domain",
            "foreign-owner",
            source_hints=("foreign.csv",),
        )
    with pytest.raises(ValueError, match="must add"):
        workspace.record_scope_discovery("customer-domain", "resolution-owner")
    workspace.release_resolution_owner("customer-domain", "resolution-owner")
    with pytest.raises(ValueError, match="does not own"):
        workspace.record_scope_discovery(
            "customer-domain",
            "resolution-owner",
            source_hints=("after-release.csv",),
        )


def test_stale_scope_submission_preserves_candidate_boundary_and_lease(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-1", "identity required")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    expected = workspace.current_scope("customer-domain").scope_hash
    workspace.record_scope_discovery(
        "customer-domain",
        "resolution-owner",
        source_hints=("customers.csv",),
    )
    state_before = (workspace.root / "state.json").read_bytes()
    with pytest.raises(StaleIdentityScopeError, match="expected scope is stale"):
        workspace.submit_result(
            "customer-domain",
            "resolution-owner",
            _accepted_result(),
            expected_scope_hash=expected,
        )
    assert (workspace.root / "state.json").read_bytes() == state_before
    domain = workspace.get_domain("customer-domain")
    assert domain.result_hash is None
    assert domain.result_scope_hash is None
    assert domain.repair_count == 0
    assert any(
        lease.owner_ref == "resolution-owner" and lease.subject_id == "customer-domain"
        for lease in workspace.active_leases
    )

    fresh = workspace.current_scope("customer-domain").scope_hash
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=fresh,
    )
    assert workspace.get_domain("customer-domain").result_scope_hash == fresh
    assert not any(lease.subject_id == "customer-domain" for lease in workspace.active_leases)


def test_scope_expansion_during_optimistic_submit_is_rejected_at_final_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-1", "identity required")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    expected = workspace.current_scope("customer-domain").scope_hash
    original_validation = workspace._base_model_for_validation
    expanded = False

    def expand_before_validation() -> LivingEnterpriseModel:
        nonlocal expanded
        if not expanded:
            expanded = True
            workspace.record_scope_discovery(
                "customer-domain",
                "resolution-owner",
                source_hints=("concurrent.csv",),
            )
        return original_validation()

    monkeypatch.setattr(workspace, "_base_model_for_validation", expand_before_validation)
    with pytest.raises(StaleIdentityScopeError, match="expected scope is stale"):
        workspace.submit_result(
            "customer-domain",
            "resolution-owner",
            _accepted_result(),
            expected_scope_hash=expected,
        )
    assert workspace.get_domain("customer-domain").result_hash is None
    assert workspace.get_domain("customer-domain").repair_count == 0
    assert any(
        lease.owner_ref == "resolution-owner" and lease.subject_id == "customer-domain"
        for lease in workspace.active_leases
    )


def test_material_request_invalidates_unreviewed_candidate_but_same_scope_does_not(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    scope_hash = workspace.current_scope("customer-domain").scope_hash
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=scope_hash,
    )
    submitted = workspace.get_domain("customer-domain")
    assert submitted.state == "review_pending"
    assert submitted.result_hash is not None

    same_scope = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-2",
        "same material identity scope",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    assert same_scope.state == "review_pending"
    assert same_scope.result_hash == submitted.result_hash

    expanded = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-3",
        "new source requires refreshed resolution",
        source_hints=("returns.csv",),
        representation_item_ids=("returns-customer-id",),
    )
    assert expanded.state == "resolving"
    assert expanded.result_hash is None
    assert expanded.result_scope_hash is None
    assert expanded.reviewer_ref is None
    assert expanded.review_verdict is None
    assert expanded.review is None
    assert expanded.accepted_pending_commit is False
    assert expanded.repair_count == 0
    assert not any(lease.subject_id == "customer-domain" for lease in workspace.active_leases)


@pytest.mark.parametrize("accepted_pending", [False, True])
def test_scope_discovery_invalidates_review_pending_candidate(
    tmp_path: Path,
    accepted_pending: bool,
) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    if accepted_pending:
        workspace.record_review("customer-domain", "accept", "independent-reviewer")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    before_repair_count = workspace.get_domain("customer-domain").repair_count

    discovered = workspace.record_scope_discovery(
        "customer-domain",
        "resolution-owner",
        source_hints=("returns.csv",),
        representation_item_ids=("returns-customer-id",),
    )
    assert discovered["status"] == "added"
    invalidated = workspace.get_domain("customer-domain")
    assert invalidated.state == "resolving"
    assert invalidated.result_hash is None
    assert invalidated.result_scope_hash is None
    assert invalidated.reviewer_ref is None
    assert invalidated.review_verdict is None
    assert invalidated.review is None
    assert invalidated.accepted_pending_commit is False
    assert invalidated.resolution_owner == "resolution-owner"
    assert invalidated.repair_count == before_repair_count == 0
    assert any(
        lease.owner_ref == "resolution-owner" and lease.subject_id == "customer-domain"
        for lease in workspace.active_leases
    )

    state_after = (workspace.root / "state.json").read_bytes()
    retry = workspace.record_scope_discovery(
        "customer-domain",
        "resolution-owner",
        source_hints=("returns.csv",),
        representation_item_ids=("returns-customer-id",),
    )
    assert retry["status"] == "already_present"
    assert (workspace.root / "state.json").read_bytes() == state_after


def test_published_old_scope_commit_reconciles_then_opens_additive_revision(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-1",
        "identity required",
        source_hints=("customers.csv",),
        representation_item_ids=("customer-id",),
    )
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    precommit = json.loads((workspace.root / "state.json").read_text(encoding="utf-8"))
    prior_commit = workspace.commit("customer-domain")

    # Simulate a crash after the immutable commit directory was published but
    # before its state binding was persisted, then an intervening scope
    # expansion by another requirement.
    crashed = dict(precommit)
    domain = dict(crashed["domains"]["customer-domain"])
    domain["requested_by"] = ["REQ-1", "REQ-2"]
    domain["requests"] = [
        *domain["requests"],
        {
            "item_id": "REQ-2",
            "object_type": "customer",
            "rationale": "expanded returns scope",
            "source_hints": ["returns.csv"],
            "representation_item_ids": ["returns-customer-id"],
        },
    ]
    domain["source_hints"] = ["customers.csv", "returns.csv"]
    domain["representation_item_ids"] = ["customer-id", "returns-customer-id"]
    crashed["domains"]["customer-domain"] = domain
    crashed["state_hash"] = _state_hash(crashed)
    crashed_bytes = (
        json.dumps(crashed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    state_path = workspace.root / "state.json"
    state_path.write_bytes(crashed_bytes)

    expanded = workspace.reserve_identity_domain(
        "customer-domain",
        "customer",
        "REQ-3",
        "another expanded scope",
        source_hints=("refunds.csv",),
        representation_item_ids=("refund-customer-id",),
    )
    assert state_path.read_bytes() != crashed_bytes
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["domains"]["customer-domain"]["state"] == "reserved"
    assert persisted["domains"]["customer-domain"]["accepted_pending_commit"] is False
    assert persisted["domains"]["customer-domain"]["revision"] == 2
    assert persisted["domains"]["customer-domain"]["published_revision"] == 1
    assert persisted["domains"]["customer-domain"]["requested_by"] == ["REQ-1", "REQ-2", "REQ-3"]
    assert expanded.commit_manifest_hash == prior_commit.manifest_hash


def test_requirement_mode_reservation_requires_materialized_exact_owner_proposal(tmp_path: Path) -> None:
    context = RunContext("RUN-REQUIREMENT-AUTH", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-1",), mode="requirement")
    workspace = EntityResolutionWorkspace.create(context)
    with pytest.raises(ValueError, match="materialized Requirement item"):
        workspace.reserve_identity_domain("domain", "customer", "REQ-1", "identity required")

    item = ItemWorkspace.create(context, "REQ-1", mode="requirement", original_text="requirement")
    with pytest.raises(ValueError, match="Analytical Owner proposal"):
        workspace.reserve_identity_domain("domain", "customer", "REQ-1", "identity required")
    item.bind_analysis_owner("ao-REQ-1")
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "domain",
            "object_type": "customer",
            "rationale": "identity required",
            "source_hints": [],
            "representation_item_ids": [],
            "item_id": "REQ-1",
            "owner_ref": "ao-REQ-1",
        }
    )
    with pytest.raises(ValueError, match="does not match"):
        workspace.reserve_identity_domain(
            "domain",
            "customer",
            "REQ-1",
            "identity required",
            request_owner_ref="fabricated-owner",
        )
    reservation = workspace.reserve_identity_domain(
        "domain",
        "customer",
        "REQ-1",
        "identity required",
    )
    assert reservation.request_records[0]["owner_ref"] == "ao-REQ-1"


def test_requirement_mode_wrong_item_mode_fails_closed_without_state_mutation(tmp_path: Path) -> None:
    context = RunContext("RUN-REQUIREMENT-WRONG-MODE", tmp_path / "run")
    RunLifecycle.create(context, ("REQ-1",), mode="requirement")
    ItemWorkspace.create(context, "REQ-1", mode="question", original_text="question item")
    workspace = EntityResolutionWorkspace.create(context)
    state_path = workspace.root / "state.json"
    before_bytes = state_path.read_bytes()
    before_state = json.dumps(dict(workspace.state), sort_keys=True, separators=(",", ":"))

    with pytest.raises(ValueError, match="mode does not match Requirement mode"):
        workspace.reserve_identity_domain(
            "domain",
            "customer",
            "REQ-1",
            "identity required",
        )

    assert state_path.read_bytes() == before_bytes
    assert json.dumps(dict(workspace.state), sort_keys=True, separators=(",", ":")) == before_state


def test_commit_reconciliation_is_all_or_nothing_across_multiple_directories(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _workspace(tmp_path)
    for domain_id in ("first", "second"):
        workspace.reserve_identity_domain(domain_id, "customer", f"Q-{domain_id}", "why")
        workspace.claim_resolution_owner(domain_id, f"owner-{domain_id}")
        workspace.submit_result(
            domain_id,
            f"owner-{domain_id}",
            _accepted_result_for(domain_id),
            expected_scope_hash=workspace.current_scope(domain_id).scope_hash,
        )
        workspace.record_review(domain_id, "accept", f"reviewer-{domain_id}")
        workspace.commit(domain_id)

    manifest_hashes: dict[str, str] = {}
    for domain_id in ("first", "second"):
        manifest_path = workspace.commits_root / hashlib.sha256(domain_id.encode("utf-8")).hexdigest() / "manifest.json"
        manifest_hashes[domain_id] = json.loads(manifest_path.read_text(encoding="utf-8"))["manifest_hash"]

    with workspace._locked():
        workspace._refresh()
        for domain_id in ("first", "second"):
            entry = dict(workspace._state["domains"][domain_id])
            entry["commit_manifest_hash"] = None
            if domain_id == "second":
                entry["result_hash"] = "b" * 64
            workspace._state["domains"][domain_id] = entry
        workspace._persist()

    state_path = workspace.root / "state.json"
    before_bytes = state_path.read_bytes()
    before_state_hash = json.loads(before_bytes)["state_hash"]
    before_public = json.dumps(dict(workspace.state), sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="result does not match the current candidate"):
        workspace.reserve_identity_domain("third", "order", "Q-third", "why")

    assert state_path.read_bytes() == before_bytes
    assert json.loads(state_path.read_bytes())["state_hash"] == before_state_hash
    assert json.dumps(dict(workspace.state), sort_keys=True, separators=(",", ":")) == before_public
    assert workspace.get_domain("first").commit_manifest_hash is None
    assert workspace.get_domain("second").commit_manifest_hash is None

    with workspace._locked():
        workspace._refresh()
        repaired = dict(workspace._state["domains"]["second"])
        repaired["result_hash"] = _accepted_result_for("second").result_hash
        workspace._state["domains"]["second"] = repaired
        workspace._persist()
    reconciled = EntityResolutionWorkspace.load(context)
    assert reconciled.get_domain("first").commit_manifest_hash == manifest_hashes["first"]
    assert reconciled.get_domain("second").commit_manifest_hash == manifest_hashes["second"]


def test_scope_discovery_persists_prior_reconciliation_before_already_present_return(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("first", "customer", "Q-first", "why")
    workspace.claim_resolution_owner("first", "owner-first")
    workspace.submit_result(
        "first",
        "owner-first",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("first").scope_hash,
    )
    workspace.record_review("first", "accept", "reviewer-first")
    workspace.commit("first")
    manifest_path = workspace.commits_root / hashlib.sha256(b"first").hexdigest() / "manifest.json"
    manifest_hash = json.loads(manifest_path.read_text(encoding="utf-8"))["manifest_hash"]

    workspace.reserve_identity_domain(
        "second",
        "order",
        "Q-second",
        "why",
        source_hints=("second.csv",),
        representation_item_ids=("second-id",),
    )
    workspace.claim_resolution_owner("second", "owner-second")
    with workspace._locked():
        workspace._refresh()
        unbound = dict(workspace._state["domains"]["first"])
        unbound["commit_manifest_hash"] = None
        workspace._state["domains"]["first"] = unbound
        workspace._persist()

    result = workspace.record_scope_discovery(
        "second",
        "owner-second",
        source_hints=("second.csv",),
        representation_item_ids=("second-id",),
    )
    assert result["status"] == "already_present"
    assert workspace.get_domain("first").commit_manifest_hash == manifest_hash
    persisted = json.loads(workspace.state_path.read_text(encoding="utf-8"))
    assert persisted["domains"]["first"]["commit_manifest_hash"] == manifest_hash
    assert persisted["domains"]["second"]["requested_by"] == ["Q-second"]


def test_reserve_expansion_persists_prior_reconciliation_and_opens_revision(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain(
        "first",
        "customer",
        "Q-first",
        "why",
        source_hints=("first.csv",),
        representation_item_ids=("first-id",),
    )
    workspace.claim_resolution_owner("first", "owner-first")
    workspace.submit_result(
        "first",
        "owner-first",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("first").scope_hash,
    )
    workspace.record_review("first", "accept", "reviewer-first")
    workspace.commit("first")
    manifest_path = workspace.commits_root / hashlib.sha256(b"first").hexdigest() / "manifest.json"
    manifest_hash = json.loads(manifest_path.read_text(encoding="utf-8"))["manifest_hash"]
    with workspace._locked():
        workspace._refresh()
        unbound = dict(workspace._state["domains"]["first"])
        unbound["commit_manifest_hash"] = None
        workspace._state["domains"]["first"] = unbound
        workspace._persist()
    before_expansion = workspace.root.joinpath("state.json").read_bytes()

    expanded = workspace.reserve_identity_domain(
        "first",
        "customer",
        "Q-second",
        "new source",
        source_hints=("new.csv",),
        representation_item_ids=("new-id",),
    )

    assert workspace.root.joinpath("state.json").read_bytes() != before_expansion
    domain = workspace.get_domain("first")
    assert domain.commit_manifest_hash == manifest_hash
    assert domain.revision == 2
    assert domain.published_revision == 1
    assert domain.state == "reserved"
    assert expanded.requested_by == ("Q-first", "Q-second")
    assert len(domain.request_records) == 2
    persisted = json.loads(workspace.state_path.read_text(encoding="utf-8"))
    assert persisted["domains"]["first"]["commit_manifest_hash"] == manifest_hash
    assert persisted["domains"]["first"]["requested_by"] == ["Q-first", "Q-second"]


def test_submit_requires_active_resolution_owner_lease(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-1", "identity required")
    lease = workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.release_worker(lease)
    with pytest.raises(ValueError, match="active resolution-owner lease"):
        workspace.submit_result(
            "customer-domain",
            "resolution-owner",
            _accepted_result(),
            expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
        )
    assert workspace.get_domain("customer-domain").result_hash is None


def test_accepted_retry_cannot_mask_tampered_result_artifact(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-1", "identity required")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    result = _accepted_result()
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        result,
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    result_path = workspace._result_path("customer-domain")
    tampered = json.loads(result_path.read_text(encoding="utf-8"))
    tampered["result_scope_hash"] = "0" * 64
    result_path.write_text(
        json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="scope binding"):
        workspace.submit_result(
            "customer-domain",
            "resolution-owner",
            result,
            expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
        )
def test_review_and_commit_reject_stale_result_scope_binding(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "REQ-1", "identity required")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    scope_hash = workspace.current_scope("customer-domain").scope_hash
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=scope_hash,
    )
    with workspace._locked():
        workspace._refresh()
        entry = dict(workspace._state["domains"]["customer-domain"])
        entry["result_scope_hash"] = "0" * 64
        workspace._state["domains"]["customer-domain"] = entry
        workspace._persist()
    with pytest.raises(StaleIdentityScopeError, match="stale scope binding"):
        workspace.record_review("customer-domain", "accept", "independent-reviewer")

    with workspace._locked():
        workspace._refresh()
        entry = dict(workspace._state["domains"]["customer-domain"])
        entry["result_scope_hash"] = scope_hash
        workspace._state["domains"]["customer-domain"] = entry
        workspace._persist()
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    with workspace._locked():
        workspace._refresh()
        entry = dict(workspace._state["domains"]["customer-domain"])
        entry["result_scope_hash"] = "0" * 64
        workspace._state["domains"]["customer-domain"] = entry
        workspace._persist()
    with pytest.raises(StaleIdentityScopeError, match="stale scope binding"):
        workspace.commit("customer-domain")


def test_empty_resolution_requires_explicit_evidenced_no_mapping_outcome(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("empty-domain", "customer", "Q-001", "bounded fixture")
    workspace.claim_resolution_owner("empty-domain", "resolution-owner")

    invalid = EntityResolutionResult(
        ontology_items=(OntologyItem(item_id="empty-customer", item_type="entity", label="Customer"),),
        coverage={"source_count": 12, "mapped_count": 0},
        population={"source_count": 12},
        source_hash=SOURCE_HASH,
    )
    with pytest.raises(ValueError, match="resolution_outcome=no_mapping_found"):
        workspace.submit_result(
            "empty-domain",
            "resolution-owner",
            invalid,
            expected_scope_hash=workspace.current_scope("empty-domain").scope_hash,
        )
    assert workspace.get_domain("empty-domain").state == "resolving"

    no_mapping = EntityResolutionResult(
        coverage={"source_count": 12, "mapped_count": 0},
        population={"source_count": 12},
        unresolved=({"reason": "no deterministic cross-source match", "row_count": 12},),
        evidence_refs=("work/no-mapping-evidence.json",),
        source_hash=SOURCE_HASH,
        metadata={"resolution_outcome": "no_mapping_found"},
    )
    workspace.submit_result(
        "empty-domain",
        "resolution-owner",
        no_mapping,
        expected_scope_hash=workspace.current_scope("empty-domain").scope_hash,
    )
    advisory = workspace.mapping_completeness_advisory()[0]
    assert advisory.status == "no_mapping_found"
    assert advisory.unresolved_record_count == 1
    workspace.record_review("empty-domain", "accept", "independent-reviewer")
    commit = workspace.commit("empty-domain")
    assert commit.record_count == 0
    assert workspace.get_domain("empty-domain").state == "ready"
    projected = replay_ready_commits(context, LivingEnterpriseModel(run_id=context.run_id)).export()
    assert projected["ontology"] == []
    assert projected["canonical_mappings"] == []


def test_canonical_relationship_roundtrip_allows_followup_integration(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001",))
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-domain", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("customer-domain", "resolution-owner")
    workspace.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("customer-domain").scope_hash,
    )
    workspace.record_review("customer-domain", "accept", "independent-reviewer")
    workspace.commit("customer-domain")

    projected = replay_ready_commits(context, LivingEnterpriseModel(run_id=context.run_id))
    exported = projected.export()
    assert LivingEnterpriseModel.from_export(exported).export() == exported
    tampered = json.loads(json.dumps(exported))
    tampered["relationships"]["customer-representation"]["target_id"] = "unknown-canonical"
    with pytest.raises(ValueError, match="relationship reference"):
        LivingEnterpriseModel.from_export(tampered)

    item = ItemWorkspace.create(context, "Q-001", original_text="follow-up")
    item.write_plan({"item_id": "Q-001", "offline": True})
    item.write_draft({"answer": "follow-up"})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json",))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    session.add_ontology_item(
        OntologyItem(item_id="follow-up-definition", item_type="definition", label="Follow-up", scope="question"),
        scope="question",
        evidence_refs=("work/plan.json",),
    )
    assert session.validate().valid
    session.record_fidelity_review("accept", checked_record_ids=tuple(record.record_id for record in session.records))
    session.commit()
    assert item.integration_state == "integrated"


def test_resolution_only_endpoints_are_replayed_before_current_relationship_commit(tmp_path: Path) -> None:
    """A current AO relationship may target a committed resolution mapping.

    The resolution workspace is run-level authority and does not require a
    duplicate item-local ontology record.  The current item's projection must
    replay that authority before applying its relationship records.
    """

    context = RunContext("RUN-RESOLUTION-ENDPOINTS", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"), mode="requirement")
    item = ItemWorkspace.create(
        context,
        "Q-001",
        mode="requirement",
        original_text="follow-up relationship",
    )
    item.bind_analysis_owner("ao-Q-001")
    item.append_identity_domain_proposal(
        {
            "record_kind": "identity_domain_proposal",
            "domain_id": "customer-domain",
            "object_type": "customer",
            "rationale": "reviewed customer identities",
            "source_hints": [],
            "representation_item_ids": [],
            "item_id": "Q-001",
            "owner_ref": "ao-Q-001",
        }
    )
    resolver = EntityResolutionWorkspace.create(context)
    resolver.reserve_identity_domain("customer-domain", "customer", "Q-001", "reviewed customer identities")
    resolver.claim_resolution_owner("customer-domain", "resolution-owner")
    resolver.submit_result(
        "customer-domain",
        "resolution-owner",
        _accepted_result(),
        expected_scope_hash=resolver.current_scope("customer-domain").scope_hash,
    )
    resolver.record_review("customer-domain", "accept", "independent-reviewer")
    resolver.commit("customer-domain")

    item = ItemWorkspace.create(context, "Q-001", mode="requirement", original_text="follow-up relationship")
    item.write_plan({"item_id": "Q-001", "offline": True})
    artifact = {
        "record_kind": "analytical_relationship",
        "relationship_id": "customer-to-canonical",
        "source_id": "customer",
        "target_id": "customer-1",
        "cardinality": "one_to_one",
        "join_keys": [{"source_field": "customer_id", "target_field": "canonical_id"}],
        "matched_pairs": 1,
        "source_population": 1,
        "target_population": 1,
        "matched_source_count": 1,
        "matched_target_count": 1,
        "source_coverage": 1.0,
        "target_coverage": 1.0,
        "date_authority": "fixture-controlled snapshot",
        "as_of": None,
        "limitations": ["Synthetic fixture only"],
        "evidence_refs": ["work/plan.json"],
        "publishable": True,
        "no_relationship_reason": None,
        "audit_id": None,
    }
    analytical_path = item.work_root / "analytical_relationships.jsonl"
    analytical_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
    item.write_draft({"answer": "one reviewed customer mapping"})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))

    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    session.add_relationship(
        {
            "relationship_id": artifact["relationship_id"],
            "analysis_relationship_id": artifact["relationship_id"],
            "source_id": artifact["source_id"],
            "target_id": artifact["target_id"],
            "cardinality": artifact["cardinality"],
            "join_keys": artifact["join_keys"],
            "matched_pairs": artifact["matched_pairs"],
            "source_population": artifact["source_population"],
            "target_population": artifact["target_population"],
            "matched_source_count": artifact["matched_source_count"],
            "matched_target_count": artifact["matched_target_count"],
            "source_coverage": artifact["source_coverage"],
            "target_coverage": artifact["target_coverage"],
            "date_authority": artifact["date_authority"],
            "as_of": artifact["as_of"],
            "limitations": artifact["limitations"],
            "evidence_refs": artifact["evidence_refs"],
        },
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
    )
    assert session.validate().valid
    session.record_fidelity_review(
        "accept",
        checked_record_ids=tuple(record.record_id for record in session.records),
    )
    session.commit()
    assert item.integration_state == "integrated"

    # The first committed item now contains a relationship whose endpoints
    # came only from run-level resolution.  A subsequent item and a full
    # projection must replay that relationship without requiring duplicate
    # ontology records in Q1.
    q2 = ItemWorkspace.create(context, "Q-002", mode="requirement", original_text="reuse relationship")
    q2.write_plan({"item_id": "Q-002", "offline": True})
    q2.write_draft({"answer": "reuse reviewed customer mapping"})
    q2.record_review("accept", reviewer_ref="reviewer")
    q2.accept(accepted_refs=("work/plan.json",))
    full_projection = LivingEnterpriseModelProjector.project(context)
    assert "customer-to-canonical" in full_projection.model.relationships
    q2_session = IntegrationSession.create(
        context,
        q2,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-002",
    )
    assert "customer-to-canonical" in q2_session.lem.relationships
    q2_session.release()


def test_unknown_relationship_endpoint_fails_preflight_atomically(tmp_path: Path) -> None:
    context = RunContext("RUN-UNKNOWN-ENDPOINT", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001",))
    item = ItemWorkspace.create(context, "Q-001", original_text="unknown endpoint")
    item.write_plan({"item_id": "Q-001", "offline": True})
    artifact = {
        "record_kind": "analytical_relationship",
        "relationship_id": "unknown-endpoint-relationship",
        "source_id": "unknown-endpoint",
        "target_id": "also-unknown",
        "cardinality": "one_to_one",
        "join_keys": [{"source_field": "id", "target_field": "id"}],
        "matched_pairs": 0,
        "source_population": 1,
        "target_population": 1,
        "matched_source_count": 0,
        "matched_target_count": 0,
        "source_coverage": 0.0,
        "target_coverage": 0.0,
        "date_authority": "fixture-controlled snapshot",
        "as_of": None,
        "limitations": ["Synthetic invalid fixture"],
        "evidence_refs": ["work/plan.json"],
        "publishable": True,
        "no_relationship_reason": None,
        "audit_id": None,
    }
    (item.work_root / "analytical_relationships.jsonl").write_text(
        json.dumps(artifact, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    item.write_draft({"answer": "bounded"})
    item.record_review("accept", reviewer_ref="reviewer")
    item.accept(accepted_refs=("work/plan.json", "work/analytical_relationships.jsonl"))
    session = IntegrationSession.create(
        context,
        item,
        PreparedAssetRegistry(context),
        "integration-owner",
        invocation_id="inv-Q-001",
    )
    session.add_relationship(
        {
            **{
                key: artifact[key]
                for key in (
                    "relationship_id",
                    "source_id",
                    "target_id",
                    "cardinality",
                    "join_keys",
                    "matched_pairs",
                    "source_population",
                    "target_population",
                    "matched_source_count",
                    "matched_target_count",
                    "source_coverage",
                    "target_coverage",
                    "date_authority",
                    "as_of",
                    "limitations",
                    "evidence_refs",
                )
            },
            "analysis_relationship_id": artifact["relationship_id"],
        },
        scope="requirement",
        evidence_refs=("work/analytical_relationships.jsonl", "work/plan.json"),
    )
    validation = session.validate()
    assert not validation.valid
    assert any("unknown ontology item or canonical mapping" in error for error in validation.errors)
    with pytest.raises(ValueError, match="integration validation failed"):
        session.record_fidelity_review(
            "accept",
            checked_record_ids=tuple(record.record_id for record in session.records),
        )
    assert item.integration_state == "pending"
    assert not session.committed_root.exists()
    assert not session.fidelity_result_path.exists()
    session.release()


def test_commit_refreshes_after_nested_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RunContext("RUN", tmp_path / "run")
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("recovered", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("recovered", "owner-recovered")
    workspace.submit_result(
        "recovered",
        "owner-recovered",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("recovered").scope_hash,
    )
    workspace.record_review("recovered", "accept", "reviewer-recovered")
    workspace.commit("recovered")

    # Model the crash window where the durable commit directory exists but the
    # state binding was not persisted yet.
    workspace._state["domains"]["recovered"]["commit_manifest_hash"] = None
    workspace._persist()
    workspace.reserve_identity_domain("new", "order", "Q-002", "why")
    workspace.claim_resolution_owner("new", "owner-new")
    workspace.submit_result(
        "new",
        "owner-new",
        _accepted_result_for("new"),
        expected_scope_hash=workspace.current_scope("new").scope_hash,
    )
    workspace.record_review("new", "accept", "reviewer-new")

    original_base = workspace._base_model_for_validation

    def nested_reconcile() -> LivingEnterpriseModel:
        EntityResolutionWorkspace.load(context)
        return original_base()

    monkeypatch.setattr(workspace, "_base_model_for_validation", nested_reconcile)
    workspace.commit("new")
    reloaded = EntityResolutionWorkspace.load(context)
    assert {domain.domain_id for domain in reloaded.domains()} == {"recovered", "new"}
    assert reloaded.get_domain("recovered").state == "ready"
    assert reloaded.get_domain("new").state == "ready"


def test_one_repair_and_three_source_mapping(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("domain", "customer", "Q-001", "why", source_hints=("a",), representation_item_ids=("r",))
    workspace.claim_resolution_owner("domain", "owner")
    workspace.submit_result(
        "domain",
        "owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("domain").scope_hash,
    )
    workspace.record_review("domain", "repair_once", "reviewer")
    workspace.claim_resolution_owner("domain", "owner")
    repaired = replace(_accepted_result(), metadata={"pattern": {"rule": "owner-supplied"}, "repair": {"rule": "owner-corrected"}})
    workspace.submit_result(
        "domain",
        "owner",
        repaired,
        expected_scope_hash=workspace.current_scope("domain").scope_hash,
    )
    workspace.record_review("domain", "accept", "reviewer")
    assert workspace.get_domain("domain").repair_count == 1
    assert workspace.commit("domain").record_count == 4


def test_requirement_wait_releases_analytical_owner_and_resumes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("domain", "customer", "Q-001", "why", source_hints=("a",), representation_item_ids=("r",))
    workspace.claim_worker("analytical_owner", "ao", "Q-001")
    workspace.mark_waiting_on_resolution("Q-001", ("domain",), "waiting for reviewed identities", owner_ref="ao")
    assert workspace.requirement_runtime_statuses()["Q-001"]["state"] == "waiting_on_resolution"
    assert not any(lease.worker_type == "analytical_owner" for lease in workspace.active_leases)

    # A real owner result, independent review, and immutable publication
    # release the wait.  Resume is not an Analytical Owner lease transition;
    # it must not recreate a released AO lease.
    workspace.claim_resolution_owner("domain", "resolution-owner")
    result = _accepted_result()
    workspace.submit_result(
        "domain",
        "resolution-owner",
        result,
        expected_scope_hash=workspace.current_scope("domain").scope_hash,
    )
    workspace.record_review("domain", "accept", "independent-reviewer")
    workspace.commit("domain")
    assert workspace.requirement_runtime_statuses()["Q-001"]["state"] == "ready_to_resume"
    resumed = workspace.acknowledge_requirement_resume("Q-001", owner_ref="ao")
    assert resumed["state"] == "resumed"
    assert not any(lease.worker_type == "analytical_owner" for lease in workspace.active_leases)


def test_representation_relationships_require_exact_source_and_target_ids(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("legacy", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("legacy", "owner")
    legacy = replace(
        _accepted_result(),
        representation_relationships=(
            {"relationship_id": "legacy", "source_ontology_id": "customer", "target_ontology_id": "customer-1"},
        ),
    )
    with pytest.raises(ValueError, match="source_id and target_id"):
        workspace.submit_result(
            "legacy",
            "owner",
            legacy,
            expected_scope_hash=workspace.current_scope("legacy").scope_hash,
        )

    # Technical result validation happens before independent review.  The
    # resolver keeps its ordinary lease and can correct the same submission;
    # no scarce business repair is consumed by a program/schema defect.
    domain = workspace.get_domain("legacy")
    assert domain.state == "resolving"
    assert domain.result_hash is None
    assert domain.repair_count == 0
    assert any(
        lease.worker_type == "entity_resolution"
        and lease.owner_ref == "owner"
        and lease.subject_id == "legacy"
        for lease in workspace.active_leases
    )

    corrected = replace(
        legacy,
        representation_relationships=(
            {
                "relationship_id": "legacy",
                "source_id": "customer",
                "target_id": "customer-1",
                "relationship_type": "represents",
            },
        ),
    )
    workspace.submit_result(
        "legacy",
        "owner",
        corrected,
        expected_scope_hash=workspace.current_scope("legacy").scope_hash,
    )
    workspace.record_review("legacy", "accept", "reviewer")
    assert workspace.commit("legacy").record_count == 4


def test_representation_relationship_source_coverage_is_not_an_analytical_edge_set(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("customer-coverage", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("customer-coverage", "owner")
    result = replace(
        _accepted_result(),
        representation_relationships=(
            {
                "relationship_id": "customer-representation",
                "source_id": "customer",
                "target_id": "customer-1",
                "relationship_type": "represents",
                "cardinality": "many_to_one",
                "source_population": 3,
                "matched_source_count": 3,
                "source_coverage": 1.0,
            },
        ),
    )
    workspace.submit_result(
        "customer-coverage",
        "owner",
        result,
        expected_scope_hash=workspace.current_scope("customer-coverage").scope_hash,
    )
    workspace.record_review("customer-coverage", "accept", "reviewer")

    commit = workspace.commit("customer-coverage")

    assert commit.record_count == 4
    projected = replay_ready_commits(
        RunContext("RUN", tmp_path / "run"),
        LivingEnterpriseModel(run_id="RUN"),
    )
    relationship = projected.relationships["customer-representation"]
    assert relationship["source_population"] == 3
    assert relationship["matched_source_count"] == 3
    assert relationship["source_coverage"] == 1.0
    exported = projected.export()
    assert LivingEnterpriseModel.from_export(exported).export() == exported


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        ({"coverage": 1.0}, "coverage is not a canonical field"),
        ({"target_population": 1}, "matched_pairs must be a non-negative integer"),
        ({"analysis_relationship_id": "analytical-edge"}, "matched_pairs must be a non-negative integer"),
        ({"source_population": 3, "source_coverage": 1.0}, "requires matched_source_count"),
        (
            {"source_population": 3, "matched_source_count": 2, "source_coverage": 1.0},
            "source_coverage is inconsistent",
        ),
        (
            {"source_population": 3, "matched_source_count": -1, "source_coverage": 0.0},
            "matched_source_count must be a non-negative integer",
        ),
    ),
)
def test_representation_relationship_partial_measurement_is_strict(
    extra: dict[str, object],
    message: str,
) -> None:
    model = LivingEnterpriseModel(run_id="RUN")
    model.add_ontology_item(OntologyItem(item_id="source", item_type="representation", label="Source"))
    model.add_ontology_item(OntologyItem(item_id="target", item_type="entity", label="Target"))
    payload = {
        "relationship_id": "source-represents-target",
        "source_id": "source",
        "target_id": "target",
        "relationship_type": "represents",
        "cardinality": "many_to_one",
        **extra,
    }
    with pytest.raises(ValueError, match=message):
        model.add_relationship(payload)


def test_representation_relationship_with_edge_measurement_uses_full_contract() -> None:
    model = LivingEnterpriseModel(run_id="RUN")
    model.add_ontology_item(OntologyItem(item_id="source", item_type="representation", label="Source"))
    model.add_ontology_item(OntologyItem(item_id="target", item_type="entity", label="Target"))
    with pytest.raises(ValueError, match="target_population must be a non-negative integer"):
        model.add_relationship(
            {
                "relationship_id": "source-represents-target",
                "source_id": "source",
                "target_id": "target",
                "relationship_type": "represents",
                "cardinality": "many_to_one",
                "matched_pairs": 1,
                "source_population": 1,
                "matched_source_count": 1,
                "source_coverage": 1.0,
            }
        )


def test_non_represents_relationship_measurement_remains_strict() -> None:
    model = LivingEnterpriseModel(run_id="RUN")
    model.add_ontology_item(OntologyItem(item_id="source", item_type="entity", label="Source"))
    model.add_ontology_item(OntologyItem(item_id="target", item_type="entity", label="Target"))
    with pytest.raises(ValueError, match="matched_pairs must be a non-negative integer"):
        model.add_relationship(
            {
                "relationship_id": "source-to-target",
                "source_id": "source",
                "target_id": "target",
                "relationship_type": "association",
                "cardinality": "many_to_one",
                "source_population": 1,
            }
        )


def test_load_reconciliation_refreshes_authoritative_state_before_persisting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash-published commit cannot erase a concurrent domain update."""
    context = RunContext("RUN", tmp_path / "run")
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("first", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("first", "owner")
    workspace.submit_result(
        "first",
        "owner",
        _accepted_result(),
        expected_scope_hash=workspace.current_scope("first").scope_hash,
    )
    workspace.record_review("first", "accept", "reviewer")
    workspace.commit("first")

    # Simulate the state half of a crash-published commit: the directory is
    # durable, but its domain binding has not yet been reflected in state.
    workspace._state["domains"]["first"]["commit_manifest_hash"] = None
    workspace._persist()
    state_path = workspace.state_path
    initial_read_started = threading.Event()
    allow_initial_read = threading.Event()
    original_read_text = Path.read_text

    def gated_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == state_path and not initial_read_started.is_set():
            initial_read_started.set()
            if not allow_initial_read.wait(timeout=5):
                raise AssertionError("load did not receive the reconciliation release")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", gated_read_text)
    loaded: list[EntityResolutionWorkspace] = []
    errors: list[BaseException] = []

    def load_workspace() -> None:
        try:
            loaded.append(EntityResolutionWorkspace.load(context))
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    thread = threading.Thread(target=load_workspace)
    thread.start()
    assert initial_read_started.wait(timeout=5)
    workspace.reserve_identity_domain("second", "order", "Q-002", "why")
    allow_initial_read.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert loaded
    reloaded = EntityResolutionWorkspace.load(context)
    assert {domain.domain_id for domain in reloaded.domains()} == {"first", "second"}
    assert reloaded.get_domain("first").state == "ready"


def test_validation_replays_prior_resolution_commits_when_lifecycle_has_a_gap(tmp_path: Path) -> None:
    context = RunContext("RUN", tmp_path / "run")
    RunLifecycle.create(context, ("Q-001", "Q-002"))
    workspace = _workspace(tmp_path)

    workspace.reserve_identity_domain("first", "customer", "Q-001", "why")
    workspace.claim_resolution_owner("first", "owner-1")
    first_result = _accepted_result()
    workspace.submit_result(
        "first",
        "owner-1",
        first_result,
        expected_scope_hash=workspace.current_scope("first").scope_hash,
    )
    workspace.record_review("first", "accept", "reviewer-1")
    workspace.commit("first")

    workspace.reserve_identity_domain("second", "customer", "Q-002", "why")
    workspace.claim_resolution_owner("second", "owner-2")
    colliding_decision = replace(first_result.identity_decisions[0], candidate_id="different-candidate")
    second_result = replace(first_result, identity_decisions=(colliding_decision,))
    with pytest.raises(ValueError, match="identity decision collision"):
        workspace.submit_result(
            "second",
            "owner-2",
            second_result,
            expected_scope_hash=workspace.current_scope("second").scope_hash,
        )
    assert workspace.get_domain("second").state == "resolving"
    assert workspace.get_domain("second").repair_count == 0


def test_stale_resolution_owner_recovery_is_explicit_audited_and_atomic(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("domain", "customer", "Q-001", "why")
    lease = workspace.claim_resolution_owner("domain", "live-owner")
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    before = workspace.state
    with pytest.raises(ValueError, match="not stale"):
        workspace.recover_resolution_owner(
            "domain",
            expected_lease_id=lease.lease_id,
            expected_owner_ref="live-owner",
            stale_before=cutoff,
            recovery_owner_ref="operator",
            reason="resolver heartbeat expired",
        )
    assert workspace.state == before

    workspace._state["leases"][0]["acquired_at"] = "2020-01-01T00:00:00+00:00"
    workspace._persist()
    audit = workspace.recover_resolution_owner(
        "domain",
        expected_lease_id=lease.lease_id,
        expected_owner_ref="live-owner",
        stale_before="2021-01-01T00:00:00+00:00",
        recovery_owner_ref="operator",
        reason="resolver heartbeat expired",
    )
    assert audit["event"] == "resolution_owner_recovered"
    assert workspace.get_domain("domain").resolution_owner is None
    assert workspace.get_domain("domain").resolution_owner_history[-1]["prior_owner_ref"] == "live-owner"
    replacement = workspace.claim_resolution_owner("domain", "replacement-owner")
    assert replacement.owner_ref == "replacement-owner"


def test_stale_resolution_owner_recovery_rejects_wrong_identity_and_missing_reason(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.reserve_identity_domain("domain", "customer", "Q-001", "why")
    lease = workspace.claim_resolution_owner("domain", "owner")
    workspace._state["leases"][0]["acquired_at"] = "2020-01-01T00:00:00+00:00"
    workspace._persist()
    with pytest.raises(ValueError, match="owner does not match"):
        workspace.recover_resolution_owner(
            "domain",
            expected_lease_id=lease.lease_id,
            expected_owner_ref="wrong-owner",
            stale_before="2021-01-01T00:00:00+00:00",
            recovery_owner_ref="operator",
            reason="resolver heartbeat expired",
        )
    with pytest.raises(ValueError, match="recovery reason"):
        workspace.recover_resolution_owner(
            "domain",
            expected_lease_id=lease.lease_id,
            expected_owner_ref="owner",
            stale_before="2021-01-01T00:00:00+00:00",
            recovery_owner_ref="operator",
            reason=" ",
        )
    assert workspace.get_domain("domain").resolution_owner == "owner"
