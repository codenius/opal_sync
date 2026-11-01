import httpx
import pytest
from bs4 import BeautifulSoup
from opal_sync.scraper.courses import parse_my_courses_html, extract_semester
from opal_sync.scraper.course_tree import parse_course_tree_html
from opal_sync.scraper.folder import fetch_folder_files
from opal_sync.scraper.file_links import extract_files_from_soup, parse_human_size
from opal_sync.scraper.wicket import WicketResponse
from opal_sync.models.node import CourseNode


def test_extract_semester():
    assert extract_semester("Mathematik 1 (WS 2026/27)") == "WS 2026/27"
    assert extract_semester("Software Architecture (SoSe 2026)") == "SoSe 2026"
    assert extract_semester("Wintersemester 2025/2026 Kurs") == "Wintersemester 2025/2026"
    assert extract_semester("No semester here") is None


def test_parse_my_courses(my_courses_html):
    courses = parse_my_courses_html(my_courses_html)
    assert len(courses) == 3

    c1 = next(c for c in courses if c.opal_id == "12345678")
    assert "Mathematik 1" in c1.name
    assert c1.semester == "WS 2026/27"
    assert c1.url == "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12345678"

    c2 = next(c for c in courses if c.opal_id == "87654321")
    assert "Software Architecture" in c2.name
    assert c2.semester == "SoSe 2026"

    c3 = next(c for c in courses if c.opal_id == "99887766")
    assert "Betriebssysteme" in c3.name


def test_parse_my_courses_ignores_truncated_course_tab_links():
    html = """
    <div>
      <a href="/opal/auth/RepositoryEntry/12345678/CourseNode/987"
         title="N021 Mathematik 1 - EIB, STB">N021 Mathematik 1 - EI...</a>
      <a href="/opal/auth/RepositoryEntry/12345678"
         title="N021 Mathematik 1 - EIB, STB">N021 Mathematik 1 - EI...</a>
    </div>
    """

    courses = parse_my_courses_html(html)

    assert len(courses) == 1
    assert courses[0].name == "N021 Mathematik 1 - EIB, STB"
    assert courses[0].url == "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12345678"


def test_parse_open_course_page_uses_full_course_title_and_root_url(fixtures_dir):
    html = (fixtures_dir / "real" / "math1_course.html").read_text(encoding="utf-8")

    courses = parse_my_courses_html(html)

    math_course = next(course for course in courses if course.opal_id == "12416188466")
    assert math_course.name == "N021 Mathematik 1 - EIB, STB"
    assert math_course.url == "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12416188466"


def test_parse_course_tree(course_folder_html):
    nodes = parse_course_tree_html(course_folder_html, course_id=1, base_url="https://bildungsportal.sachsen.de")
    assert len(nodes) == 3

    folder_nodes = [n for n in nodes if n.node_type == "folder"]
    assert len(folder_nodes) == 2
    assert folder_nodes[0].node_id == "101"
    assert folder_nodes[0].title == "Vorlesungsunterlagen"

    sp_node = next(n for n in nodes if n.node_type == "singlepage")
    assert sp_node.node_id == "103"
    assert sp_node.title == "Organisatorisches"


def test_parse_course_tree_json_only_includes_structure_and_folder_icons():
    html = """
    <script>
    var initial_data = [
      {"id":"id1","text":"Root","icon":"fonticon icon-root","a_attr":{"href":"/CourseNode/1"}},
      {"id":"id2","text":"Materials","icon":"fonticon icon-st","a_attr":{"href":"/CourseNode/2"},
       "children":[
         {"id":"id3","text":"Folder","icon":"fonticon icon-bc","a_attr":{"href":"/CourseNode/3"}}
       ]},
      {"id":"id4","text":"Page","icon":"fonticon icon-sp","a_attr":{"href":"/CourseNode/4"}},
      {"id":"id5","text":"Task","icon":"fonticon icon-ta","a_attr":{"href":"/CourseNode/5"}},
      {"id":"id6","text":"Looks like folder, wrong icon","icon":"fonticon icon-sp",
       "li_attr":{"class":"node-bc"},"a_attr":{"href":"/CourseNode/6"}}
    ];
    </script>
    """

    nodes = parse_course_tree_html(
        html,
        course_id=1,
        base_url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1",
    )

    assert [(node.node_id, node.node_type) for node in nodes] == [
        ("2", "structure"),
        ("3", "folder"),
    ]
    assert [node.relative_path for node in nodes] == ["Materials", "Materials/Folder"]


def test_parse_course_tree_preserves_structure_ancestry_and_omits_root_unterlagen():
    html = """
    <script>
    var initial_data = [
      {"id":"id0","text":"Course root","icon":"fonticon icon-root","a_attr":{"href":"/CourseNode/0"},
       "children":[
        {"id":"id1","text":"Unterlagen","icon":"fonticon icon-st","a_attr":{"href":"/CourseNode/1"},
          "children":[
            {"id":"id2","text":"Kursmaterialien","icon":"fonticon icon-bc","a_attr":{"href":"/CourseNode/2"}}
          ]},
        {"id":"id3","text":"Material zur Lehrveranstaltung","icon":"fonticon icon-st",
         "a_attr":{"href":"/CourseNode/3"},"children":[
          {"id":"id4","text":"Physik I","icon":"fonticon icon-st","a_attr":{"href":"/CourseNode/4"},
           "children":[
             {"id":"id5","text":"1_1 Einführung","icon":"fonticon icon-bc","a_attr":{"href":"/CourseNode/5"}}
           ]}
          ]}
       ]}
    ];
    </script>
    """

    nodes = parse_course_tree_html(
        html,
        course_id=1,
        base_url="https://example.com/course",
    )

    assert [(node.title, node.relative_path) for node in nodes] == [
        ("Unterlagen", ""),
        ("Kursmaterialien", "Kursmaterialien"),
        ("Material zur Lehrveranstaltung", "Material zur Lehrveranstaltung"),
        (
            "Physik I",
            "Material zur Lehrveranstaltung/Physik I",
        ),
        (
            "1_1 Einführung",
            "Material zur Lehrveranstaltung/Physik I/1_1 Einführung",
        ),
    ]


def test_parse_human_size():
    assert parse_human_size("1.5 MB") == int(1.5 * 1024 * 1024)
    assert parse_human_size("450 KB") == 450 * 1024
    assert parse_human_size("200 Bytes") == 200
    assert parse_human_size("Invalid") is None


def test_extract_files_accepts_unlisted_extensions_and_applies_filter_priority():
    soup = BeautifulSoup(
        """
        <div>
          <a href="/files/model.blend">model.blend</a>
          <a href="/files/archive.tar.gz">archive.tar.gz</a>
        </div>
        """,
        "html.parser",
    )

    files = extract_files_from_soup(
        soup,
        "https://example.com/",
        course_id=1,
    )
    assert [file.filename for file in files] == ["model.blend", "archive.tar.gz"]

    filtered_files = extract_files_from_soup(
        soup,
        "https://example.com/",
        course_id=1,
        include_extensions=[".blend"],
        exclude_extensions=[".blend", ".gz"],
    )
    assert [file.filename for file in filtered_files] == ["model.blend"]


def test_extract_files_does_not_treat_footer_version_text_as_file():
    soup = BeautifulSoup(
        """
        <main>
          <a href="/files/model.blend">model.blend</a>
        </main>
        <footer>
          <a href="/wiki/releases/OPAL">Über OPAL 2026.10.1_ N11</a>
          <a href="/downloads/release-notes.pdf">Release notes</a>
        </footer>
        """,
        "html.parser",
    )

    files = extract_files_from_soup(
        soup,
        "https://example.com/",
        course_id=1,
    )

    assert [file.filename for file in files] == ["model.blend"]


def test_extract_files_from_table_accepts_unlisted_extension():
    soup = BeautifulSoup(
        '<table><tr><td><a href="/files/model.blend">model.blend</a></td></tr></table>',
        "html.parser",
    )

    files = extract_files_from_soup(
        soup,
        "https://example.com/",
        course_id=1,
    )

    assert [file.filename for file in files] == ["model.blend"]


def test_extract_files_can_return_files_excluded_by_extension_filter():
    soup = BeautifulSoup(
        '<table><tr><td><a href="/files/lecture.mp4">lecture.mp4</a></td></tr></table>',
        "html.parser",
    )

    files = extract_files_from_soup(
        soup,
        "https://example.com/",
        course_id=1,
        exclude_extensions=[".mp4"],
        include_excluded=True,
    )

    assert [file.filename for file in files] == ["lecture.mp4"]


def test_extract_files_from_soup(course_folder_html):
    soup = BeautifulSoup(course_folder_html, "html.parser")
    files = extract_files_from_soup(
        soup=soup,
        base_url="https://bildungsportal.sachsen.de",
        course_id=1,
        node_id=101,
        allowed_extensions=[".pdf", ".pptx"]
    )

    assert len(files) == 3
    filenames = [f.filename for f in files]
    assert "Kapitel1.pdf" in filenames
    assert "Kapitel2_Slides.pptx" in filenames
    assert "Formelsammlung.pdf" in filenames

    f1 = next(f for f in files if f.filename == "Kapitel1.pdf")
    assert f1.size_bytes == int(1.5 * 1024 * 1024)
    assert f1.last_modified == "15.10.2026"


def test_extract_wicket_file_download_request(fixtures_dir):
    html = (fixtures_dir / "real" / "math1_kursmaterialien.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    files = extract_files_from_soup(
        soup=soup,
        base_url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12416188466/CourseNode/94390817219110",
        course_id=1,
        allowed_extensions=[".pdf"],
    )

    file_entry = next(f for f in files if f.filename == "Ergebnisse_Test_2026.pdf")
    assert file_entry.last_modified == "am 09.10.2026 um 11:47 Uhr"
    assert file_entry.download_request is not None
    assert "tableMultiActionRepeater-1-tableMultiAction" in file_entry.download_request.url
    assert file_entry.download_request.data[
        "contentContainer:tableContainer:multiSelect:multiSelectGroup"
    ] == "check13"
    assert file_entry.download_request.data[
        "tableMultiActionContainer:tableMultiActionRepeater:1:tableMultiAction"
    ] == ""
    assert file_entry.download_request.headers["Wicket-Ajax"] == "true"
    assert file_entry.download_request.headers["Wicket-Ajax-BaseURL"].startswith(
        "auth/RepositoryEntry/"
    )


def test_extract_files_ignores_recent_file_references_on_opal_folder(fixtures_dir):
    html = (fixtures_dir / "real" / "math1_kursmaterialien.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    recent_files = soup.new_tag("div", attrs={"class": "recent-files"})
    recent_files.append(
        soup.new_tag(
            "a",
            href=(
                "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/"
                "12416188466/CourseNode/94390817219110/Beleg_1.pdf"
            ),
        )
    )
    recent_files.a.string = "Beleg_1.pdf"
    soup.body.append(recent_files)

    files = extract_files_from_soup(
        soup=soup,
        base_url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12416188466/CourseNode/94390817219110",
        course_id=1,
        allowed_extensions=[".pdf"],
    )

    assert [file.filename for file in files] == ["Ergebnisse_Test_2026.pdf"]
    assert files[0].download_request is not None


def test_extract_files_without_extension_filter_ignores_folder_rows(fixtures_dir):
    html = (fixtures_dir / "real" / "math1_kursmaterialien.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    files = extract_files_from_soup(
        soup=soup,
        base_url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/12416188466/CourseNode/94390817219110",
        course_id=1,
        allowed_extensions=None,
    )

    assert [file.filename for file in files] == ["Ergebnisse_Test_2026.pdf"]


def test_extract_files_ignores_email_address_links():
    html = '<div><a href="/silvia.paketuris-scholer@htwk-leipzig.de">silvia.paketuris-scholer@htwk-leipzig.de</a></div>'
    files = extract_files_from_soup(
        soup=BeautifulSoup(html, "html.parser"),
        base_url="https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1",
        course_id=1,
    )

    assert files == []


@pytest.mark.asyncio
async def test_nested_folder_listing_uses_its_actual_file_url():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    nested_url = root_url + "/Vorlesungsmaterialien"
    redirected_nested_url = nested_url + "?view=1"
    file_url = root_url + "/Vorlesungsmaterialien/Vorlesung_06.pdf"
    root_html = """
    <table><tr><td>Ordner</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Vorlesungsmaterialien">
    Vorlesungsmaterialien</a></td></tr></table>
    """
    nested_html = """
    <table><tr><td>PDF-Dokument</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Vorlesungsmaterialien/Vorlesung_06.pdf">
    Vorlesung_06.pdf</a></td></tr></table>
    """
    node = CourseNode(
        course_id=1,
        node_id="10",
        title="Kursmaterialien",
        url=root_url,
        relative_path="Material zur Lehrveranstaltung/Physik I/Kursmaterialien",
    )

    async def handler(request):
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html, request=request)
        if str(request.url) == nested_url:
            return httpx.Response(
                302,
                headers={"Location": redirected_nested_url},
                request=request,
            )
        if str(request.url) == redirected_nested_url:
            return httpx.Response(200, text=nested_html, request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await fetch_folder_files(client, node, allowed_extensions=[".pdf"])

    assert len(files) == 1
    assert files[0].remote_url == file_url
    assert files[0].remote_path == (
        "Material zur Lehrveranstaltung/Physik I/Kursmaterialien/"
        "Vorlesungsmaterialien/Vorlesung_06.pdf"
    )


@pytest.mark.asyncio
async def test_folder_navigation_uses_wicket_link_action_and_keeps_paths():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    folder_url = root_url + "/Seminarmaterialien+EIB1"
    folder2_url = root_url + "/Seminarmaterialien+EIB2"
    file_url = folder_url + "/Seminar_01.pdf"
    file2_url = folder2_url + "/Seminar_02.pdf"
    root_html = f"""
    <html><body>
    <form id="root-form">
      <table>
        <tr><td><input type="checkbox" name="multiSelectGroup" value="folder"></td>
        <td><span title="Ordner"></span>
        <a id="folder-link" href="{folder_url}">Seminarmaterialien EIB1</a></td></tr>
        <tr><td><input type="checkbox" name="multiSelectGroup" value="folder2"></td>
        <td><span title="Ordner"></span>
        <a id="folder2-link" href="{folder2_url}">Seminarmaterialien EIB2</a></td></tr>
        <tr><td><input type="checkbox" name="multiSelectGroup" value="root-file"></td>
        <td><span title="PDF-Dokument"></span>
        <a href="{root_url}/Ergebnisse_Test_2026.pdf">Ergebnisse_Test_2026.pdf</a></td></tr>
      </table>
      <input type="submit" name="tableMultiAction" value="Download">
    </form>
    <script>
      Wicket.Ajax.baseUrl = "auth/RepositoryEntry/1/CourseNode/10";
      Wicket.Ajax.ajax({{"u":"/opal/auth/RepositoryEntry/1/CourseNode/10?7-1.1-folderLink","c":"folder-link","e":"click"}});
      Wicket.Ajax.ajax({{"u":"/opal/auth/RepositoryEntry/1/CourseNode/10?7-1.1-folder2Link","c":"folder2-link","e":"click"}});
    </script>
    </body></html>
    """
    folder_component = f"""
    <form id="child-form">
      <table>
        <tr><td><input type="checkbox" name="multiSelectGroup" value="seminar"></td>
        <td><span title="PDF-Dokument"></span>
        <a href="{file_url}">Seminar_01.pdf</a></td></tr>
      </table>
      <input id="download-button" type="submit"
        name="tableMultiActionContainer:tableMultiActionRepeater:1:tableMultiAction"
        value="Download" title="Download">
    </form>
    """
    ajax_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <ajax-response>
      <component id="listing"><![CDATA[{folder_component}]]></component>
      <evaluate><![CDATA[
        Wicket.Ajax.ajax({{"u":"/opal/auth/RepositoryEntry/1/CourseNode/10?8-download","m":"POST","c":"download-button","f":"child-form","sc":"download-submit"}});
      ]]></evaluate>
    </ajax-response>
    """
    folder2_component = folder_component.replace(file_url, file2_url).replace(
        "Seminar_01.pdf", "Seminar_02.pdf"
    )
    ajax_response2 = ajax_response.replace(folder_component, folder2_component)
    node = CourseNode(course_id=1, node_id="10", title="Kursmaterialien", url=root_url)
    requested_urls = []

    async def handler(request):
        requested_urls.append(str(request.url))
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html, request=request)
        if "7-1.1-folderLink" in str(request.url):
            assert request.headers["Wicket-Ajax"] == "true"
            assert request.headers["Wicket-Ajax-BaseURL"] == "auth/RepositoryEntry/1/CourseNode/10"
            return httpx.Response(200, text=ajax_response, request=request)
        if "7-1.1-folder2Link" in str(request.url):
            assert request.headers["Wicket-Ajax"] == "true"
            return httpx.Response(200, text=ajax_response2, request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await fetch_folder_files(client, node, allowed_extensions=[".pdf"])

    assert [file.filename for file in files] == [
        "Ergebnisse_Test_2026.pdf",
        "Seminar_01.pdf",
        "Seminar_02.pdf",
    ]
    seminar = files[1]
    assert seminar.remote_url == file_url
    assert seminar.remote_path == "Kursmaterialien/Seminarmaterialien EIB1/Seminar_01.pdf"
    assert seminar.download_request is not None
    assert files[2].remote_url == file2_url
    assert files[2].remote_path == "Kursmaterialien/Seminarmaterialien EIB2/Seminar_02.pdf"
    assert requested_urls.count(root_url) == 3
    assert folder_url not in requested_urls
    assert any("7-1.1-folderLink" in url for url in requested_urls)
    assert any("7-1.1-folder2Link" in url for url in requested_urls)


@pytest.mark.asyncio
async def test_unchanged_folder_is_not_opened_again():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    child_url = root_url + "/Vorlesungsmaterialien"
    modified = "am 27.10.2026 um 12:44 Uhr"
    root_html = f"""
    <form id="folder-form">
      <table>
        <thead><tr>
          <th></th><th></th><th></th><th></th><th></th>
          <th data-header="table.folder.lastModified">Zuletzt geändert</th>
        </tr></thead>
        <tbody>
          <tr>
            <td><input type="checkbox" name="multiSelectGroup" value="folder"></td>
            <td><span title="Ordner"></span>
              <a href="{child_url}">Vorlesungsmaterialien</a>
            </td>
            <td></td><td></td><td></td><td>{modified}</td>
          </tr>
        </tbody>
      </table>
      <input type="submit" name="tableMultiAction" value="Download">
    </form>
    """
    node = CourseNode(course_id=1, node_id="10", title="Kursmaterialien", url=root_url)
    requested_urls = []

    async def handler(request):
        requested_urls.append(str(request.url))
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html, request=request)
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        skipped_paths = set()
        files = await fetch_folder_files(
            client,
            node,
            allowed_extensions=[".pdf"],
            known_folder_states={
                child_url: ("Kursmaterialien/Vorlesungsmaterialien", modified)
            },
            skipped_folder_paths=skipped_paths,
        )

    assert files == []
    assert requested_urls
    assert set(requested_urls) == {root_url}
    assert skipped_paths == {"Kursmaterialien/Vorlesungsmaterialien"}


@pytest.mark.asyncio
async def test_folder_redirect_to_parent_does_not_nest_sibling_listing():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    sibling_url = root_url + "/Seminarmaterialien+EIB1"
    root_html = """
    <table><tr><td>Ordner</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB1">
    Seminarmaterialien EIB1</a></td></tr>
    <tr><td>PDF-Dokument</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Ergebnisse_Test_2026.pdf">
    Ergebnisse_Test_2026.pdf</a></td></tr></table>
    """
    node = CourseNode(
        course_id=1,
        node_id="10",
        title="Kursmaterialien",
        url=root_url,
    )

    async def handler(request):
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html, request=request)
        if str(request.url) == sibling_url:
            return httpx.Response(
                302,
                headers={"Location": root_url + "?state=2"},
                request=request,
            )
        if str(request.url) == root_url + "?state=2":
            return httpx.Response(200, text=root_html, request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await fetch_folder_files(client, node, allowed_extensions=[".pdf"])

    assert len(files) == 1
    assert files[0].filename == "Ergebnisse_Test_2026.pdf"
    assert files[0].remote_path == "Kursmaterialien/Ergebnisse_Test_2026.pdf"
    assert files[0].remote_url == root_url + "/Ergebnisse_Test_2026.pdf"


@pytest.mark.asyncio
async def test_sibling_folders_with_unique_contents_are_both_crawled():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    eib1_url = root_url + "/Seminarmaterialien+EIB1"
    eib2_url = root_url + "/Seminarmaterialien+EIB2"
    root_html = """
    <table>
    <tr><td>Ordner</td><td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB1">Seminarmaterialien EIB1</a></td></tr>
    <tr><td>Ordner</td><td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB2">Seminarmaterialien EIB2</a></td></tr>
    </table>
    """
    eib1_html = """
    <table><tr><td>PDF-Dokument</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB1/EIB1.pdf">EIB1.pdf</a></td></tr></table>
    """
    eib2_html = """
    <table><tr><td>PDF-Dokument</td>
    <td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB2/EIB2.pdf">EIB2.pdf</a></td></tr></table>
    """
    node = CourseNode(
        course_id=1,
        node_id="10",
        title="Kursmaterialien",
        url=root_url,
    )

    async def handler(request):
        response_by_url = {
            root_url: root_html,
            eib1_url: eib1_html,
            eib2_url: eib2_html,
        }
        html = response_by_url.get(str(request.url))
        if html is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await fetch_folder_files(client, node, allowed_extensions=[".pdf"])

    assert {file.filename for file in files} == {"EIB1.pdf", "EIB2.pdf"}
    assert {file.remote_path for file in files} == {
        "Kursmaterialien/Seminarmaterialien EIB1/EIB1.pdf",
        "Kursmaterialien/Seminarmaterialien EIB2/EIB2.pdf",
    }


@pytest.mark.asyncio
async def test_identical_sibling_listings_are_not_skipped():
    root_url = "https://bildungsportal.sachsen.de/opal/auth/RepositoryEntry/1/CourseNode/10"
    eib1_url = root_url + "/Seminarmaterialien+EIB1"
    eib2_url = root_url + "/Seminarmaterialien+EIB2"
    root_html = """
    <table>
    <tr><td>Ordner</td><td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB1">Seminarmaterialien EIB1</a></td></tr>
    <tr><td>Ordner</td><td><a href="/opal/auth/RepositoryEntry/1/CourseNode/10/Seminarmaterialien+EIB2">Seminarmaterialien EIB2</a></td></tr>
    </table>
    """
    sibling_html = """
    <table><tr><td>PDF-Dokument</td>
    <td><a href="Vorlesung_06.pdf">Vorlesung_06.pdf</a></td></tr></table>
    """
    node = CourseNode(
        course_id=1,
        node_id="10",
        title="Kursmaterialien",
        url=root_url,
    )

    async def handler(request):
        response_by_url = {
            root_url: root_html,
            eib1_url: sibling_html,
            eib2_url: sibling_html,
        }
        html = response_by_url.get(str(request.url))
        if html is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await fetch_folder_files(client, node, allowed_extensions=[".pdf"])

    assert {file.remote_path for file in files} == {
        "Kursmaterialien/Seminarmaterialien EIB1/Vorlesung_06.pdf",
        "Kursmaterialien/Seminarmaterialien EIB2/Vorlesung_06.pdf",
    }


def test_wicket_response_parser():
    xml_data = """<?xml version="1.0" encoding="UTF-8"?>
    <ajax-response>
        <component id="comp1"><![CDATA[<div class="test">Hello Wicket</div>]]></component>
        <component id="comp2"><![CDATA[<p>Content 2</p>]]></component>
        <redirect><![CDATA[/opal/auth/next]]></redirect>
    </ajax-response>
    """
    wicket = WicketResponse(xml_data)
    assert "comp1" in wicket.components
    assert "Hello Wicket" in wicket.components["comp1"]
    assert wicket.redirect_url == "/opal/auth/next"
    assert "Content 2" in wicket.get_combined_html()
