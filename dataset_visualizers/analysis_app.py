import sys; sys.path.insert(0, "..")
import streamlit as st
import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from PIL import Image
import scipy.io
import base64
from io import BytesIO
import torch
import torch.nn as nn
import deepgaze_pytorch  # Make sure you have the DeepGazeIII PyTorch package installed
from scipy.ndimage import zoom
from scipy.special import logsumexp
from helper_loaders import *
import json


# Track the selected image
if "selected_image_idx" not in st.session_state:
    st.session_state.selected_image_idx = 0  # Initialize with the first image of the current batch

if "selected_subjects_list" not in st.session_state:
    st.session_state.selected_subjects_list = []

if "clearing" not in st.session_state:
    st.session_state.clearing = False

if "adding_all" not in st.session_state:
    st.session_state.adding_all = False

if "computation_options" not in st.session_state:
    st.session_state.computation_options = {
        "show_map": False,
        "compute_fixations": False,
        "compute_fixation_crops": False,
        "compute_deepgaze": False,        
    }

if "changing_dataset" not in st.session_state:
    st.session_state.changing_dataset = False

# ************************************** #
# *******   HELPER FUNCTIONS  ********** #
# ************************************** #
# Function to convert a PIL image to base64 string
def pil_to_base64(img):
    buffered = BytesIO()
    img.save(buffered, format="JPEG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return img_str

# Caching function for loading image paths
@st.cache_data
def load_image_paths(stimuli_folder):
    """Load and cache image file paths."""
    return [f for f in os.listdir(stimuli_folder) if f.endswith('.jpeg') or f.endswith('.jpg')]

# Caching function for loading a batch of thumbnails
@st.cache_data
def load_thumbnails(image_paths, batch_size=10):
    """Load and cache thumbnails for a given batch."""
    thumbnails = []
    for img_path in image_paths[:batch_size]:
        full_img_path = os.path.join(stimuli_folder, img_path)
        image = Image.open(full_img_path)
        image.thumbnail((100, 100))  # Create a small thumbnail
        thumbnails.append(image)
    return thumbnails

@st.cache_resource
def load_deepgaze_model(device='cpu'):
    """Load and cache the DeepGazeIII model."""
    model = deepgaze_pytorch.DeepGazeIII(pretrained=True).to(torch.float32).to(device)
    centerbias_template = np.load('../centerbias_mit1003.npy')
        
    return model, centerbias_template

# Function to calculate fixation points using different algorithms
def get_fixation(x, y, time_interval, algorithm_mode=0, velocity_threshold=1000, dispersion_threshold=25, min_fixation_duration=0.1):
    def calculate_velocity(x, y, time_interval):
        velocities = [np.sqrt((x[i] - x[i - 1])**2 + (y[i] - y[i - 1])**2) / time_interval for i in range(1, len(x))]
        return velocities

    def apply_ivt(x, y, time_interval, velocity_threshold, min_fixation_duration):
        velocities = calculate_velocity(x, y, time_interval)
        labels = np.zeros(len(x))  # 0 = Saccade, 1 = Fixation
        current_fixation = []

        for i in range(len(velocities)):
            if velocities[i] < velocity_threshold:
                current_fixation.append(i + 1)  # +1 due to velocities being shorter
            else:
                if len(current_fixation) * time_interval >= min_fixation_duration:
                    labels[current_fixation] = 1
                current_fixation = []
        return labels

    def apply_idt(x, y, time_interval, dispersion_threshold, min_fixation_duration):
        labels = np.zeros(len(x))
        i = 0
        while i < len(x):
            j = i
            while j < len(x) and (max(x[i:j + 1]) - min(x[i:j + 1])) < dispersion_threshold and (max(y[i:j + 1]) - min(y[i:j + 1])) < dispersion_threshold:
                j += 1
            if (j - i) * time_interval >= min_fixation_duration:
                labels[i:j] = 1
            i = j
        return labels

    # Apply both algorithms and combine using the selected mode
    ivt_labels = apply_ivt(x, y, time_interval, velocity_threshold, min_fixation_duration)
    idt_labels = apply_idt(x, y, time_interval, dispersion_threshold, min_fixation_duration)
    
    if algorithm_mode == 0:
        fixation_labels = np.logical_and(ivt_labels, idt_labels).astype(int)  # AND mode
    elif algorithm_mode == 1:
        fixation_labels = np.logical_or(ivt_labels, idt_labels).astype(int)  # OR mode
    elif algorithm_mode == 2:
        fixation_labels = ivt_labels  # I-VT only
    elif algorithm_mode == 3:
        fixation_labels = idt_labels  # I-DT only
    else:
        raise ValueError("Invalid algorithm mode. Use 0 (AND), 1 (OR), 2 (I-VT), or 3 (I-DT).")

    # Extract fixation sections
    fixation_sections, avg_x, avg_y = [], [], []
    current_fixation = []

    for i, label in enumerate(fixation_labels):
        if label == 1:
            current_fixation.append((x[i], y[i]))
        else:
            if current_fixation:
                fixation_sections.append(current_fixation)
                avg_x.append(np.mean([p[0] for p in current_fixation]))
                avg_y.append(np.mean([p[1] for p in current_fixation]))
                current_fixation = []

    if current_fixation:
        fixation_sections.append(current_fixation)
        avg_x.append(np.mean([p[0] for p in current_fixation]))
        avg_y.append(np.mean([p[1] for p in current_fixation]))

    return fixation_sections, avg_x, avg_y


# Sidebar configuration
# Set the paths for the stimuli, data, and affiliated image folders
base_folder = '../Datasets/'
    
datasets = [f for f in os.listdir(base_folder) if os.path.isdir(os.path.join(base_folder, f))][::-1]

# Selection for the dataset
def dataset_switching_callback():
    st.session_state.changing_dataset = True
selected_dataset = st.sidebar.selectbox("Dataset", datasets, on_change=dataset_switching_callback)

stimuli_folder = os.path.join(base_folder, selected_dataset, "ALLSTIMULI")
data_folder = os.path.join(base_folder, selected_dataset, "DATA")
fixation_folder = os.path.join(base_folder, selected_dataset, 'ALLFIXATIONMAPS')
subject_folders = [f for f in os.listdir(data_folder) if os.path.isdir(os.path.join(data_folder, f))]

# Load Dataset Configs
dataset_configs = ""
with open('dataset_config.json', 'r') as file:
    dataset_configs = json.load(file)
dataset_config = dataset_configs['datasets'][selected_dataset]
print(dataset_config)

# Parameters for fixation detection
sample_rate = dataset_config['Sample Rate']
algorithm_mode_index = 0
time_interval = 1 / sample_rate  # 240 Hz sampling rate
velocity_threshold = 1000  # I-VT velocity threshold (pixels/second)
dispersion_threshold = 25  # I-DT spatial dispersion threshold (pixels)
min_fixation_duration = 0.1  # Minimum fixation duration (seconds)


## Image Gallery in the Side Bar

# Load image paths
image_files = load_image_paths(stimuli_folder)
st.sidebar.write("Gallery")

# Implement pagination for the gallery
total_images = len(image_files)
batch_size = 10  # Number of thumbnails to load at a time
num_batches = (total_images + batch_size - 1) // batch_size

# Sidebar slider to select the current batch
batch_index = st.sidebar.slider("Select Batch", 0, num_batches - 1, 0)
start_idx = batch_index * batch_size
end_idx = min(start_idx + batch_size, total_images)

# Load thumbnails for the current batch
thumbnails = load_thumbnails(image_files[start_idx:end_idx])

# Display thumbnails in a fixed-height container with custom grid layout
st.sidebar.write("### Image Thumbnails (Scroll)")
thumbnail_container = st.sidebar.container()

# Create a fixed grid layout with custom box size
thumbnail_cols = thumbnail_container.columns(5)  # Fixed 5 columns for layout

# Display thumbnails with uniform box size and clickable buttons
for i, (img, caption) in enumerate(zip(thumbnails, image_files[start_idx:end_idx])):
    with thumbnail_cols[i % 5]:
        # Convert PIL image to base64 string
        img_base64 = pil_to_base64(img)
        
        # 使用 Streamlit 原生的 HTML 格式化来嵌入 Base64 图片
        st.markdown(
            f"""
            <div style='border: 1px solid #ccc; width: 100px; height: 100px; display: flex; align-items: center; justify-content: center;'>
                <img src="data:image/jpeg;base64,{img_base64}" style="max-width: 100px; max-height: 100px;"/>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # 显示图像，并使用 Streamlit 原生组件来处理点击事件
        if st.button(f"Select {i}", key=f"btn_{start_idx + i}"):
            st.session_state.selected_image_idx = start_idx + i  # 更新选中的图像索引
            st.rerun()  # 强制刷新页面，更新选中状态


# Update the selected image based on the clicked button index
selected_image_idx = st.session_state.selected_image_idx
selected_image = image_files[selected_image_idx]

# Display selected image information in the sidebar
st.sidebar.write(f"**Selected Image**: {selected_image}")

# Display the main image name
st.write(f"## Selected Image: {selected_image}")

# Load selected image
image_path = os.path.join(stimuli_folder, selected_image)
img = mpimg.imread(image_path)


## SUBJECT SELECTION 
# subject_folders_unadded = []
# for item in subject_folders:
#     if item not in st.session_state.selected_subjects_list:
#         subject_folders_unadded.append(item)

# print(subject_folders_unadded)
# if len(subject_folders_unadded) > 0:
#     subject_to_add = container.selectbox(
#         "Select an subject to add to the display",
#         subject_folders_unadded,
#         index = None if st.session_state.clearing else 1,
#     )
#     if subject_to_add is not None and subject_to_add not in st.session_state.selected_subjects_list:
#         st.session_state.selected_subjects_list.append(subject_to_add)
#         if st.session_state.clearing:
#             st.session_state.clearing = False 

default_options = []
if "multiselect_0" in st.session_state:
    default_options = st.session_state.multiselect_0
else:
    default_options = st.session_state.selected_subjects_list

if st.session_state.adding_all:
    default_options = subject_folders
    st.session_state.adding_all = False

if st.session_state.changing_dataset:
    default_options = []
    st.session_state.changing_dataset = False

st.sidebar.multiselect(
    "select subjects",
    subject_folders,
    default_options,
    key = "multiselect_0"
)
st.session_state.selected_subjects_list = st.session_state.multiselect_0

if st.sidebar.button("select all subjects"):
    st.session_state.selected_subjects_list = subject_folders
    st.session_state.adding_all = True
    st.rerun()

# 显示选中的 subjects
combined_subjects_string = " | ".join([f"`{subject[:4]}`" for subject in st.session_state.selected_subjects_list])
st.write(f"**Selected Subjects:** {combined_subjects_string}")

# Options for visualization

st.session_state.computation_options['show_map'] = st.sidebar.checkbox('Show Map', value=st.session_state.computation_options['show_map'])
st.session_state.computation_options['compute_fixations'] = st.sidebar.checkbox('Compute Fixations', value= st.session_state.computation_options['compute_fixations'])
st.session_state.computation_options['compute_fixation_crops'] = st.sidebar.checkbox('Compute Fixation Crops', value=st.session_state.computation_options['compute_fixation_crops'])
st.session_state.computation_options['compute_deepgaze'] = False
if st.session_state.computation_options['compute_fixations']:
    st.session_state.computation_options['compute_deepgaze'] = st.sidebar.checkbox('Compute DeepGaze',value=st.session_state.computation_options['compute_deepgaze'])

# st.write(st.session_state.computation_options)

# Algorithm selection (only visible if Compute Fixations is enabled)
if st.session_state.computation_options['compute_fixations']:
    algorithm_mode_index = st.sidebar.selectbox(
        'Algorithm Mode', 
        range(4), 
        format_func=lambda x: ['AND', 'OR', 'I-VT', 'I-DT'][x]
    )

    # Use an expander to group fixation parameters when Compute Fixations is enabled
    with st.sidebar.expander("Fixation Detection Parameters", expanded=False):
        # Slider for Velocity Threshold
        velocity_threshold = st.slider(
            "Velocity Threshold (I-VT, pixels/second)", 
            min_value=100, max_value=2000, value=1000, step=50
        )

        # Slider for Dispersion Threshold
        dispersion_threshold = st.slider(
            "Dispersion Threshold (I-DT, pixels)", 
            min_value=5, max_value=100, value=25, step=5
        )

        # Slider for Minimum Fixation Duration
        min_fixation_duration = st.slider(
            "Minimum Fixation Duration (seconds)", 
            min_value=0.01, max_value=0.5, value=0.1, step=0.01
        )


# Step 1: Calculate fixation data for all selected subjects (if enabled)
fixations_data = {}  # Store computed fixations for each subject
max_fixation_count = 0
data_loader = data_loaders[dataset_config['Data Loader']]


for subject in st.session_state.selected_subjects_list:
    data_path = os.path.join(data_folder, subject, f'{selected_image.split(".")[0]}.{dataset_config["Data Suffix"]}')
    if os.path.exists(data_path):
        x, y = data_loader(data_path, image_path)        
        
        # Calculate fixations if enabled
        if st.session_state.computation_options['compute_fixations']:
            fixation_sections, x, y = get_fixation(x, y, time_interval, algorithm_mode=algorithm_mode_index,
                                                velocity_threshold=velocity_threshold, 
                                                dispersion_threshold=dispersion_threshold, 
                                                min_fixation_duration=min_fixation_duration)
            fixations_data[subject] = (fixation_sections, x, y)
        else:
            fixations_data[subject] = ([], x, y)  # Use raw scanpaths without fixation

        # Update the maximum fixation count for layout adjustment
        max_fixation_count = max(max_fixation_count, len(x))


## ********************************************* #
## *****  IMAGE No.1 : MAIN VIUSALIZAITON  ***** #
## ********************************************* #

# Step 2: Create the main visualization figure with scanpaths and optional fixation overlays
fig, ax = plt.subplots(figsize=(12, 8))

# Get the original dimensions
orig_width, orig_height = img.shape[:2][::-1]
# st.sidebar.write(f"image width: {orig_width}, image height: {orig_height}")

# Calculate the scaling factor to fit within the specified max dimensions
width_ratio = dataset_config['Window Width'] / orig_width
height_ratio = dataset_config['Window Height'] / orig_height
scaling_factor = min(width_ratio, height_ratio)

# Calculate new dimensions while preserving the aspect ratio
new_width = int(orig_width * scaling_factor)
new_height = int(orig_height * scaling_factor)

# Resize the image if it's larger than the specified dimensions
# if scaling_factor < 1:
#     # Use np.interp for resizing
#     img = np.array(Image.fromarray((img).astype(np.uint8)).resize((new_width, new_height)))
# else:
#     img = img  # No resizing needed if the image fits within the max dimensions

ax.imshow(img)  # Display the main stimulus image

fixmap_path = os.path.join(fixation_folder, f'{selected_image.split(".")[0]}_fixMap.jpg')
fixmap_img = mpimg.imread(fixmap_path) if st.session_state.computation_options['show_map'] and os.path.exists(fixmap_path) else None
if fixmap_img is not None:
    ax.imshow(fixmap_img, cmap='hot', alpha=0.5)

# Overlay scanpaths for each subject
colors = plt.cm.get_cmap('hsv', len(st.session_state.selected_subjects_list)+1)
colors_dict = {}
for idx, subject in enumerate(st.session_state.selected_subjects_list):
    colors_dict[subject] = colors(idx)
    fixation_sections, x, y = fixations_data[subject]
    # Use either raw data or computed fixation points
    ax.plot(x, y, marker='o', color=colors(idx), linewidth=1, markersize=4, label=f'{subject}')

# Show legend and main image plot
ax.legend(loc='upper right')
st.pyplot(fig)  # Render the main figure with scanpaths


## ********************************************* #
## *****    IMAGE No.2 : FIXATION CROPS    ***** #
## ********************************************* #

if st.session_state.computation_options['compute_fixation_crops']:
    # Step 3: Create a second figure for cropped fixation regions visualization
    n = st.sidebar.slider('Field of View Size', 20, 100, 50, 10)  # Slider to control cropped region size
    margin = 0.0015

    # Create a new figure for sequential fixation regions with dynamic width based on fixation count
    fig_width = max_fixation_count  # Use dynamic width based on maximum fixation count
    fig2, ax2 = plt.subplots(figsize=(fig_width, len(st.session_state.selected_subjects_list)))

    # Loop through each subject and visualize cropped regions
    for row_idx, subject in enumerate(st.session_state.selected_subjects_list):
        fixation_sections, x, y = fixations_data[subject]  # Retrieve the stored fixation data

        # Determine dynamic width and height for each cropped region
        total_width = img.shape[1]  # Total width of the original image
        total_height = img.shape[0]  # Total height of the original image
        crop_width = n / total_width  # Width of each cropped region in normalized coordinates
        crop_height = n / total_height  # Height of each cropped region in normalized coordinates

        # Display each cropped region as a separate image in a row for this subject
        for col_idx, (fx, fy) in enumerate(zip(x, y)):
            # Define crop boundaries in the original image
            x1, x2 = int(fx - n // 2), int(fx + n // 2)
            y1, y2 = int(fy - n // 2), int(fy + n // 2)
            
            # Ensure boundaries are within the original image dimensions
            x1, x2 = max(0, x1), min(img.shape[1], x2)
            y1, y2 = max(0, y1), min(img.shape[0], y2)

            # Crop the region and display
            cropped_img = img[y1:y2, x1:x2]

            # Calculate the extent based on `col_idx`, `row_idx` and dynamic crop width/height
            extent_left = col_idx * crop_width + margin
            extent_right = (col_idx + 1) * crop_width - margin
            extent_bottom = row_idx * crop_height + margin
            extent_top = (row_idx + 1) * crop_height - margin

            # Display the cropped image using dynamic extent
            ax2.imshow(cropped_img, extent=[extent_left, extent_right, extent_bottom, extent_top], aspect='auto')
        
        # Add subject name as text on the left of each row
        ax2.text(-0.01, (row_idx + 0.5) * crop_height, f'{subject}', 
                         verticalalignment='center', horizontalalignment='right', fontsize=12, bbox=dict(facecolor='white', alpha=0.5)) 

    # Set limits and formatting for the subplot using dynamic limits
    ax2.set_xlim(-crop_width, max_fixation_count * crop_width)  # Adjust the x limit based on the maximum number of fixations
    ax2.set_ylim(0, len(st.session_state.selected_subjects_list) * crop_height)  # Adjust the y limit according to the number of subjects
    ax2.set_xticks([])
    ax2.set_yticks([])

    # Set up title and labels
    ax2.set_title("Sequential Fixation Regions for Each Subject")
    ax2.set_xlabel("Fixation Time Sequence (normalized)")
    ax2.set_ylabel("Subjects")

    # Show the new plot with cropped regions
    st.pyplot(fig2)



## ************************************************ #
## ***** IMAGE No.3 : DEEPGAZE VISUALIZATIONS ***** #
## ************************************************ #

# Select a subject for DeepGazeIII prediction from the sidebar
if st.session_state.computation_options['compute_deepgaze']:
    deepgaze_subject = st.sidebar.selectbox('Select a Subject for DeepGazeIII Prediction', st.session_state.selected_subjects_list)

    # Check if CUDA is available for GPU acceleration, or use CPU
    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

    # Load the pre-trained DeepGazeIII model
    model, centerbias_template = load_deepgaze_model(DEVICE)

    # Display the selected image and DeepGazeIII visualization if applicable
    if  os.path.exists(image_path) and deepgaze_subject:
        # Ensure the image is in the correct format with 3 channels (RGB)
        img = mpimg.imread(image_path)
        if img.shape[-1] == 4:  # Drop alpha channel if present
            img = img[:, :, :3]

        # Load and extract fixation data for the selected DeepGaze subject
        deepgaze_data_path = os.path.join(data_folder, deepgaze_subject, f'{selected_image.split(".")[0]}.{dataset_config["Data Suffix"]}')
        print(deepgaze_data_path)
        if os.path.exists(deepgaze_data_path):
            x, y = data_loader(deepgaze_data_path, image_path)
            
            # Use `get_fixation` to obtain the fixation points for DeepGazeIII prediction
            fixation_sections, deepgaze_x, deepgaze_y = get_fixation(x, y, time_interval, algorithm_mode=algorithm_mode_index,
                                                    velocity_threshold=velocity_threshold, 
                                                    dispersion_threshold=dispersion_threshold, 
                                                    min_fixation_duration=min_fixation_duration)

            # Slider for selecting the starting fixation index for DeepGazeIII prediction
            fixation_start_idx = st.sidebar.slider('Select Starting Fixation Index for DeepGazeIII', 
                                                min_value=0, 
                                                max_value=max(0, len(deepgaze_x) - 4), 
                                                value=0, 
                                                step=1)

            # Convert the image into a tensor format
            image_tensor = torch.tensor([img.transpose(2, 0, 1)]).float().to(DEVICE)

            # Load the centerbias template and adjust it to the image dimensions
            centerbias = zoom(centerbias_template, (img.shape[0] / centerbias_template.shape[0], 
                                                    img.shape[1] / centerbias_template.shape[1]), 
                                                    order=0, mode='nearest')
            centerbias -= logsumexp(centerbias)
            centerbias_tensor = torch.tensor([centerbias]).float().to(DEVICE)

            # Use a range of fixations starting from the selected index
            x_hist_tensor = torch.tensor([deepgaze_x[fixation_start_idx:fixation_start_idx + 4]]).float().to(DEVICE)
            y_hist_tensor = torch.tensor([deepgaze_y[fixation_start_idx:fixation_start_idx + 4]]).float().to(DEVICE)

            # Perform the prediction using the selected fixations
            with torch.no_grad():
                log_density_prediction = model(image_tensor, centerbias_tensor, x_hist_tensor, y_hist_tensor)

            # Step 3: Create a new figure for DeepGazeIII visualization
            fig3, axs = plt.subplots(nrows=1, ncols=2, figsize=(14, 6))
            
            # Original Image with Fixations
            axs[0].imshow(img)
            axs[0].plot(deepgaze_x[0:fixation_start_idx + 4], deepgaze_y[0:fixation_start_idx + 4], 'o-', color=colors_dict[deepgaze_subject])
            axs[0].scatter(deepgaze_x[fixation_start_idx + 3], deepgaze_y[fixation_start_idx + 3], 100, color='yellow')
            axs[0].set_title('Fixation Scanpath')
            axs[0].set_axis_off()

            # Predicted Heatmap
            predicted_heatmap = log_density_prediction.detach().cpu().numpy()[0, 0]
            axs[1].imshow(img, alpha=0.5)
            heatmap = axs[1].imshow(predicted_heatmap, cmap='jet', alpha=0.6)
            axs[1].plot(deepgaze_x[fixation_start_idx:fixation_start_idx + 4], deepgaze_y[fixation_start_idx:fixation_start_idx + 4], 'o-', color='red')
            axs[1].scatter(deepgaze_x[fixation_start_idx + 3], deepgaze_y[fixation_start_idx + 3], 100, color='yellow')
            axs[1].set_title('DeepGazeIII Predicted Heatmap')
            axs[1].set_axis_off()
            # fig3.colorbar(heatmap, ax=axs[1])
            
            # Display the DeepGazeIII prediction using Streamlit
            st.pyplot(fig3)
