package com.grocery.booking.grocery.Service;

import com.grocery.booking.grocery.Entity.GroceryItem;
import com.grocery.booking.grocery.DTO.InventoryManagementRequest;
import com.grocery.booking.grocery.Exception.ResourceNotFoundException;
import com.grocery.booking.grocery.Repository.GroceryItemRepository;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class GroceryItemService {
    @Autowired
    private GroceryItemRepository groceryItemRepository;

    public List<GroceryItem> getAllGroceryItems() {
        return groceryItemRepository.findAll();
    }

    public GroceryItem addGroceryItem(GroceryItem groceryItem) {
        return groceryItemRepository.save(groceryItem);
    }

    public GroceryItem updateGroceryItem(Long id, GroceryItem updatedItem) {
        // Implement update logic
        // You can use JpaRepository's save method to update the entity
        GroceryItem existingItem = groceryItemRepository.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("GroceryItem not found with id: " + id));

        existingItem.setName(updatedItem.getName());
        existingItem.setPrice(updatedItem.getPrice());
        existingItem.setQuantity(updatedItem.getQuantity());

        return groceryItemRepository.save(existingItem);
    }

    public void deleteGroceryItem(Long id) {
        // Implement delete logic
        // You can use JpaRepository's deleteById method to delete the entity
        groceryItemRepository.deleteById(id);
    }

    public GroceryItem manageInventory(Long id, InventoryManagementRequest request) {
        // Implement inventory management logic
        // Depending on the request, increase or decrease the quantity
        GroceryItem existingItem = groceryItemRepository.findById(id)
                .orElseThrow(() -> new ResourceNotFoundException("GroceryItem not found with id: " + id));

        if ("increase".equals(request.getOperation())) {
            existingItem.setQuantity(existingItem.getQuantity() + request.getQuantity());
        } else if ("decrease".equals(request.getOperation())) {
            int newQuantity = existingItem.getQuantity() - request.getQuantity();
            if (newQuantity < 0) {
                throw new IllegalArgumentException("Inventory cannot be decreased below 0");
            }
            existingItem.setQuantity(newQuantity);
        } else {
            throw new IllegalArgumentException("Invalid operation: " + request.getOperation());
        }

        return groceryItemRepository.save(existingItem);
    }

    public List<GroceryItem> getAllAvailableGroceryItems() {
        return groceryItemRepository.findByQuantityGreaterThan(0);
    }

    // Other methods for updating, deleting, managing inventory, etc.
}
