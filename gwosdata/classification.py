"""Canonical city classification and explicit merge rules for warehouse publication."""

from __future__ import annotations

import re


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


_MERGE_GROUPS: dict[str, tuple[str, ...]] = {
    "Delhi": ("Faridabad",),
    "Trivandrum": ("Attingal",),
    "Vijayawada": ("Guntur",),
}

EXPLICIT_MERGES: dict[str, str] = {
    _slug(source): _slug(canonical)
    for canonical, sources in _MERGE_GROUPS.items()
    for source in sources
}

MERGED_SOURCE_CITIES: dict[str, tuple[str, ...]] = {
    _slug(canonical): tuple(sorted((canonical, *sources)))
    for canonical, sources in _MERGE_GROUPS.items()
}

_TIER_CITIES: dict[str, tuple[str, ...]] = {
    "T1": (
        'Bangalore', 'Chennai', 'Delhi', 'Hyderabad', 'Jaipur', 'Kolkata',
        'Mumbai',
    ),
    "T2": (
        'Ahmedabad', 'Bhopal', 'Bhubaneswar', 'Chandigarh', 'Coimbatore', 'Guwahati',
        'Indore', 'Kanpur', 'Kochi', 'Lucknow', 'Meerut', 'Nagpur',
        'Nashik', 'Patna', 'Pune', 'Rajkot', 'Surat', 'Vadodara',
        'Varanasi', 'Vijayawada', 'Vishakapatnam',
    ),
    "T3": (
        'Abohar', 'Adilabad', 'Agartala', 'Agra', 'Ahilyanagar', 'Aizawl',
        'Ajmer', 'Akola', 'Aligarh', 'Almora', 'Alwar', 'Ambala',
        'Ambedkar Nagar', 'Ambikapur', 'Ambur', 'Amethi', 'Amreli', 'Amritsar',
        'Amroha', 'Anand_Nadiad', 'Anantapur', 'Angul', 'Ankleshwar_Bharuch', 'Arrah',
        'Asansol', 'Ashoknagar', 'Auraiya', 'Aurangabad', 'Aurangabad_Bihar', 'Ayodhya',
        'Azamgarh', 'Bagalkote', 'Baghpat', 'Bahraich', 'Balaghat', 'Balasore',
        'Ballari', 'Ballia', 'Balotra', 'Balurghat', 'Banda', 'Bangaon',
        'Baran', 'Bareilly', 'Baripada', 'Barmer', 'Barnala', 'Basirhat',
        'Basti', 'Batala', 'Bathinda', 'Beawar', 'Begusarai', 'Belgaum',
        'Bettiah', 'Betul', 'Bhadohi', 'Bhadrak', 'Bhagalpur', 'Bharatpur',
        'Bhavnagar', 'Bhilai', 'Bhilwara', 'Bhimavaram', 'Bhind', 'Bhinmal',
        'Bhiwadi', 'Bhiwani', 'Bhuj', 'Bhuvangiri', 'Bidar', 'Bihar Sharif',
        'Bijnor', 'Bikaner', 'Bilaspur', 'Bokaro Steel', 'Bongaigaon', 'Botad',
        'Brahmapur', 'Budaun', 'Bulandshahr', 'Burhanpur', 'Buxar', 'Chaibasa',
        'Chakdaha', 'Chamarajnagar', 'Chandauli', 'Chandausi', 'Charkhi Dadri', 'Chhapra',
        'Chhatarpur', 'Chhindwara', 'Chikkaballapur', 'Chikkamagaluru', 'Chilakaluripet', 'Chitradurga',
        'Chitrakoot', 'Chittoor', 'Chittorgarh', 'Churu', 'Cuddalore', 'Dahod',
        'Damoh', 'Dankuni', 'Darbhanga', 'Datia', 'Dausa', 'Davangere',
        'Deeg', 'Dehradun', 'Dehri', 'Deoband', 'Deoghar', 'Deoria',
        'Dessa', 'Dewas', 'Dhanbad', 'Dhar', 'Dharmapuri', 'Dhenkanal',
        'Dholpur', 'Dhubri', 'Dhuliyan', 'Dibrugarh', 'Digha', 'Dindigul',
        'Dumka', 'Dungarpur', 'Durgapur', 'Dwarka', 'Eluru', 'Erode',
        'Etah', 'Etawah', 'Faridkot', 'Farrukhabad', 'Fatehabad', 'Fatehgarh Sahib',
        'Fatehpur', 'Firozabad', 'Firozpur', 'Forbesganj', 'Gadag', 'Gandhidham',
        'Gangapur City', 'Garhwa', 'Gaya', 'Ghazipur', 'Giridih', 'Godhra',
        'Gohana', 'Gonda', 'Gondal', 'Gopalganj', 'Gorakhpur', 'Gudivada',
        'Guna', 'Gurdaspur', 'Gwalior', 'Habra', 'Haldwani', 'Halol',
        'Hamirpur', 'Hansi', 'Hanumangarh', 'Hapur', 'Harda', 'Hardoi',
        'Hassan', 'Hathras', 'Haveri', 'Hazaribagh', 'Himatnagar', 'Hindaun',
        'Hisar', 'Hosapete', 'Hoshiarpur', 'Hosur', 'Hubballidharwad', 'Itarsi',
        'Jabalpur', 'Jagdalpur Bastar', 'Jaisalmer', 'Jalandhar', 'Jalna', 'Jammu',
        'Jamnagar', 'Jamshedpur', 'Jamui', 'Jangipur', 'Jaunpur', 'Jehanabad',
        'Jetpur', 'Jhajjar', 'Jhalawar', 'Jhansi', 'Jhargram', 'Jhunjhunu',
        'Jind', 'Jodhpur', 'Jorhat', 'Junagadh', 'Kadapa', 'Kaithal',
        'Kakinada', 'Kalaburagi', 'Kamareddy', 'Kanchipuram', 'Kannauj', 'Kannur',
        'Karaikudi', 'Karimnagar', 'Karnal', 'Karur', 'Kasganj', 'Kashipur',
        'Katihar', 'Katni', 'Katwa', 'Khammam', 'Khandwa', 'Kharagpur',
        'Khargone', 'Kolar', 'Kollam', 'Koppal', 'Korba', 'Kota',
        'Kotdwar', 'Kothagudem', 'Kozhikode', 'Krishnanagar', 'Kumbakonam', 'Kurnool',
        'Kurukshetra', 'Lakhimpurkheri', 'Lalitpur', 'Ludhiana', 'Machilipatnam', 'Madanapalle',
        'Madhubani', 'Madhupur', 'Madurai', 'Maharajganj', 'Mahasamund', 'Mahbubnagar',
        'Mahendragarh', 'Mahoba', 'Mainpuri', 'Malappuram', 'Malda', 'Malerkotla',
        'Mandla', 'Mandsaur', 'Mandya', 'Mangalore', 'Mathura', 'Mau',
        'Medininagar', 'Mehsana', 'Mirzapur', 'Modasa', 'Moga', 'Moradabad',
        'Morbi', 'Morena', 'Morinda', 'Motihari', 'Munger', 'Murshidabad',
        'Muzaffarnagar', 'Muzaffarpur', 'Mysore', 'Nabadwip', 'Nagaon', 'Nagaur',
        'Nagda', 'Nagercoil', 'Nainital', 'Nalagarh', 'Nalgonda', 'Namakal',
        'Nandyala', 'Narasaraopet', 'Narmadapuram', 'Narwana', 'Navsari', 'Neemuch',
        'Nellore', 'Nizamabad', 'Nuh', 'Omkareshwar', 'Ongole', 'Orai',
        'Palakkad', 'Palanpur', 'Palghar', 'Pali', 'Panipat', 'Patan',
        'Pathankot', 'Patiala', 'Perambalur', 'Phalodi', 'Phusro Bermo Bokaro Thermal', 'Pilibhit',
        'Pollachi', 'Porbandar', 'Pratapgarh', 'Pratapgarh_Up', 'Prayagraj', 'Puducherry',
        'Purba Medinipur', 'Purulia', 'Raebareli', 'Raichur', 'Raigarh', 'Raipur',
        'Raisen', 'Rajahmundry', 'Rajapalayam', 'Rajnandgaon', 'Rajpura', 'Rajsamand',
        'Ramagundam', 'Ramanagara', 'Ramanathapuram', 'Rampur', 'Ranchi', 'Ratlam',
        'Rewa', 'Rewari', 'Robertsganj', 'Rohtak', 'Rourkela', 'Rupnagar',
        'Sagar', 'Saharanpur', 'Saharsa', 'Salem', 'Samastipur', 'Sambalpur',
        'Sambhal', 'Sangareddy', 'Sangrur', 'Sant Kabir Nagar', 'Sasaram', 'Satara',
        'Satna', 'Sattenapalle', 'Sawai Madhopur', 'Sehore', 'Seoni', 'Shahdol',
        'Shaheed Bhagat Singh Nagar', 'Shahjahanpur', 'Shajapur', 'Shamli', 'Sheohar', 'Shillong',
        'Shimla', 'Shivamogga', 'Shivpuri', 'Siddipet', 'Sidhi', 'Sikar',
        'Silchar', 'Siliguri', 'Sirmaur', 'Sirsa', 'Sitamarhi', 'Sitapur',
        'Siwan', 'Solan', 'Somnath', 'Sri Ganganagar', 'Sri Muktsar Sahib', 'Srikakulam',
        'Srinagar', 'Sultanpur', 'Supaul', 'Surendranagar', 'Suryapet', 'Tandur',
        'Tarn Taran', 'Tezpur', 'Thanjavur', 'Theni', 'Thoothukudi', 'Thorrur',
        'Thrissur', 'Tikamgarh', 'Tinsukia', 'Tirunelveli', 'Tirupati', 'Tiruppur',
        'Tiruvalluru', 'Tiruvannamalai', 'Tohana', 'Trichy', 'Trivandrum', 'Tumakuru',
        'Tura', 'Udaipur', 'Udaipur Tri', 'Udupi', 'Ujjain', 'Valsad',
        'Vapi', 'Vellore', 'Vidisha', 'Vijayapura', 'Villupuram', 'Virudhunagar',
        'Vizianagaram', 'Warangal', 'Yamuna Nagar',
    ),
}

CANONICAL_CITIES: dict[str, tuple[str, str]] = {
    _slug(city): (city, tier)
    for tier, cities in _TIER_CITIES.items()
    for city in cities
}

UNATTRIBUTED_CITY_KEY = "city_not_found"
UNATTRIBUTED_SOURCE_KEYS = frozenset({UNATTRIBUTED_CITY_KEY, "unrecognized_city"})


def normalize_city_key(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return UNATTRIBUTED_CITY_KEY
    slugged = _slug(cleaned)
    if not slugged or slugged in UNATTRIBUTED_SOURCE_KEYS:
        return UNATTRIBUTED_CITY_KEY
    return EXPLICIT_MERGES.get(slugged, slugged)


def resolve_city(canonical_key: str, sample_source_city: str | None = None) -> tuple[str, str, str, list[str]]:
    key = normalize_city_key(canonical_key)
    if key == UNATTRIBUTED_CITY_KEY:
        # Preserve the storage placeholder; this is not an attributed T3 city.
        return UNATTRIBUTED_CITY_KEY, UNATTRIBUTED_CITY_KEY, "T3", [UNATTRIBUTED_CITY_KEY]
    if key in CANONICAL_CITIES:
        display, tier = CANONICAL_CITIES[key]
        lineage = list(MERGED_SOURCE_CITIES.get(key, (display,)))
        return key, display, tier, lineage
    sample = (sample_source_city or "").strip()
    if not sample or normalize_city_key(sample) != key:
        sample = key
    return key, sample, "T3", [sample]
