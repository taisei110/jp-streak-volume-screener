from html2image import Html2Image
hti = Html2Image()
hti.screenshot(html_file='screen_result.html', save_as='screen_result.png', size=(1400, 1800))
print("Successfully created screen_result.png")
